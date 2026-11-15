"""AI extraction engine: PDF -> validated ``PurchaseOrder``.

Pipeline
1. PyMuPDF reads the PDF. If it has a usable text layer we send the text;
   if it is a scan (little or no text) we render the pages to PNG and send
   them as images, so Claude reads them multimodally.
2. Claude is forced to call a single tool whose ``input_schema`` is the
   Pydantic JSON schema. This guarantees a structured JSON object, which is
   then validated again with Pydantic (one automatic repair retry).
3. Demo/Mock mode: with no ``ANTHROPIC_API_KEY`` the extractor returns
   pre-cached payloads from ``samples/mock_payloads/<pdf-stem>.json``, so the
   whole system can be demoed offline at zero cost.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pymupdf as fitz  # PyMuPDF
from pydantic import ValidationError

import config
from models import PurchaseOrder

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 80          # below this we treat the PDF as a scan
MAX_PAGES = 5                # cost guard-rail
RENDER_DPI = 150
TOOL_NAME = "record_purchase_order"

SYSTEM_PROMPT = """You are a deterministic purchase-order data extraction engine for an ERP integration.

Your ONLY output is a single call to the `record_purchase_order` tool. Never write prose.

Rules:
1. Extract exactly what is printed. Do NOT invent, infer, normalise or correct values.
2. PO_Number: the buyer's purchase order number (labels like "PO #", "P.O. No.", "Order Number",
   "Purchase Order"). If it is not printed, return null. Never use invoice, quote or reference numbers.
3. Customer_Name: the BUYER company issuing the PO (not the vendor/supplier receiving it).
4. Ship_To: the full delivery address on one line, comma separated. null if absent.
5. Line_Items: one entry per ordered product line, in document order. Ignore subtotal, tax,
   freight and total rows.
   - SKU: the item/part/catalog code exactly as printed (customer or vendor code); null if none.
   - Description: the item description as printed.
   - Qty: numeric quantity ordered.
   - UOM: unit of measure if printed, else null.
   - Unit_Price: numeric unit price without currency symbols or thousands separators.
     If only an extended amount is printed, compute Unit_Price = amount / Qty.
6. Numbers must be JSON numbers, never strings.
"""


class ExtractionError(RuntimeError):
    pass


class DemoPayloadNotFound(ExtractionError):
    pass


@dataclass
class PDFContent:
    text: str
    images_png: list[bytes]
    page_count: int

    @property
    def is_scanned(self) -> bool:
        return len(self.text.strip()) < MIN_TEXT_CHARS


@dataclass
class ExtractionOutput:
    order: PurchaseOrder
    mode: str               # "live" | "demo"
    model: Optional[str]
    raw: dict


# --------------------------------------------------------------- PDF reading
def read_pdf(pdf: bytes | str | Path, render_images: Optional[bool] = None) -> PDFContent:
    """Extract the text layer and (for scans, or when forced) page images."""
    doc = fitz.open(stream=pdf, filetype="pdf") if isinstance(pdf, (bytes, bytearray)) else fitz.open(pdf)
    try:
        pages = list(doc)[:MAX_PAGES]
        text = "\n\n".join(f"--- Page {i + 1} ---\n{p.get_text('text')}" for i, p in enumerate(pages))
        content = PDFContent(text=text, images_png=[], page_count=doc.page_count)
        if render_images or (render_images is None and content.is_scanned):
            content.images_png = [p.get_pixmap(dpi=RENDER_DPI).tobytes("png") for p in pages]
        return content
    finally:
        doc.close()


# ---------------------------------------------------------------- extractor
class POExtractor:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 force_demo: bool = False):
        self.api_key = api_key if api_key is not None else config.ANTHROPIC_API_KEY
        self.model = model or config.ANTHROPIC_MODEL
        self.demo = force_demo or not self.api_key
        self._client = None
        if not self.demo:
            import anthropic  # imported lazily so demo mode has no hard dependency on network
            self._client = anthropic.Anthropic(api_key=self.api_key)

    @property
    def mode(self) -> str:
        return "demo" if self.demo else "live"

    # public ---------------------------------------------------------------
    def extract(self, pdf: bytes | str | Path, filename: Optional[str] = None) -> ExtractionOutput:
        name = filename or (Path(pdf).name if not isinstance(pdf, (bytes, bytearray)) else "upload.pdf")
        if self.demo:
            return self._extract_demo(name)
        return self._extract_live(read_pdf(pdf), name)

    # demo -------------------------------------------------------------------
    def _extract_demo(self, filename: str) -> ExtractionOutput:
        payload_path = config.MOCK_PAYLOADS_DIR / f"{Path(filename).stem}.json"
        if not payload_path.exists():
            available = sorted(p.stem for p in config.MOCK_PAYLOADS_DIR.glob("*.json"))
            raise DemoPayloadNotFound(
                f"Demo mode: no pre-cached payload for '{filename}'. Set ANTHROPIC_API_KEY for live "
                f"extraction, or use one of the bundled samples: {', '.join(available)}"
            )
        raw = json.loads(payload_path.read_text(encoding="utf-8"))
        return ExtractionOutput(order=PurchaseOrder.model_validate(raw), mode="demo", model=None, raw=raw)

    # live -------------------------------------------------------------------
    def _tool(self) -> dict:
        return {
            "name": TOOL_NAME,
            "description": "Record the structured data of one purchase order.",
            "input_schema": PurchaseOrder.model_json_schema(by_alias=True),
        }

    def _build_content(self, pdf: PDFContent, filename: str) -> list[dict]:
        blocks: list[dict] = []
        for png in pdf.images_png:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.standard_b64encode(png).decode()},
            })
        intro = f"Source file: {filename} ({pdf.page_count} page(s))."
        if pdf.is_scanned:
            intro += " The document is a scanned image; read it from the page images above."
        else:
            intro += f"\n\nPDF text layer:\n<document>\n{pdf.text}\n</document>"
        blocks.append({"type": "text", "text": intro + "\n\nExtract the purchase order."})
        return blocks

    def _call(self, messages: list[dict]):
        return self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=[self._tool()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=messages,
        )

    def _extract_live(self, pdf: PDFContent, filename: str) -> ExtractionOutput:
        messages: list[dict] = [{"role": "user", "content": self._build_content(pdf, filename)}]
        last_error: Optional[Exception] = None
        for attempt in range(2):  # 1 call + 1 self-repair retry
            response = self._call(messages)
            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use is None:
                raise ExtractionError(f"Model returned no tool call (stop_reason={response.stop_reason})")
            try:
                order = PurchaseOrder.model_validate(tool_use.input)
                return ExtractionOutput(order=order, mode="live", model=self.model, raw=tool_use.input)
            except ValidationError as exc:
                last_error = exc
                log.warning("Schema validation failed (attempt %s): %s", attempt + 1, exc)
                messages += [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_use.id, "is_error": True,
                        "content": f"Schema validation failed, call the tool again with corrected data:\n{exc}",
                    }]},
                ]
        raise ExtractionError(f"Extraction failed schema validation: {last_error}")


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(next(config.SAMPLE_PDFS_DIR.glob("*.pdf")))
    out = POExtractor().extract(target)
    print(f"[{out.mode}] {target}")
    print(json.dumps(out.order.to_wire(), indent=2))
