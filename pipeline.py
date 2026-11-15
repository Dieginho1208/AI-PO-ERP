"""Orchestration: PDF -> extract -> validate -> persist -> (CSV if approved).

CLI:  python pipeline.py [folder_or_pdf ...]   (defaults to samples/pdfs)
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
import database
from exporter import export_csv
from extractor import POExtractor
from models import PurchaseOrder, Status, ValidationResult
from validator import POValidator

log = logging.getLogger("pipeline")


@dataclass
class ProcessedOrder:
    order_id: int
    source_file: str
    result: ValidationResult
    csv_path: Optional[Path]
    mode: str


class Pipeline:
    def __init__(self, extractor: Optional[POExtractor] = None, db_path: Optional[Path] = None):
        database.init_db(db_path=db_path)
        self.db_path = db_path
        self.extractor = extractor or POExtractor()
        self.validator = POValidator(db_path=db_path)

    def process_pdf(self, pdf: bytes | str | Path, filename: Optional[str] = None) -> ProcessedOrder:
        name = filename or Path(pdf).name
        extraction = self.extractor.extract(pdf, filename=name)
        return self._finalise(name, extraction.order, extraction.mode)

    def revalidate(self, order_id: int, order: PurchaseOrder, source_file: str, mode: str,
                   reviewer: str = "operator", force_approve: bool = False) -> ProcessedOrder:
        """Re-run the rules after an operator edit, optionally overriding remaining flags."""
        result = self.validator.validate(order, order_id=order_id)
        if force_approve and result.status != Status.APPROVED:
            result.status = Status.APPROVED
        return self._finalise(source_file, order, mode, order_id=order_id, reviewer=reviewer,
                              result=result, override=force_approve)

    def _finalise(self, name: str, order: PurchaseOrder, mode: str, order_id: Optional[int] = None,
                  reviewer: Optional[str] = None, result: Optional[ValidationResult] = None,
                  override: bool = False) -> ProcessedOrder:
        result = result or self.validator.validate(order, order_id=order_id)
        csv_path = export_csv(result, operator_override=override) if result.status == Status.APPROVED else None
        oid = database.save_order(name, result.model_dump(mode="json"), mode,
                                  csv_path=str(csv_path) if csv_path else None,
                                  order_id=order_id, reviewed_by=reviewer, db_path=self.db_path)
        return ProcessedOrder(oid, name, result, csv_path, mode)


def _iter_pdfs(targets: list[str]):
    for t in targets:
        p = Path(t)
        yield from (sorted(p.glob("*.pdf")) if p.is_dir() else [p])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Batch-process purchase order PDFs")
    ap.add_argument("targets", nargs="*", default=[str(config.SAMPLE_PDFS_DIR)])
    ap.add_argument("--reset", action="store_true", help="reset the ERP DB before running")
    args = ap.parse_args()
    if args.reset:
        database.init_db(reset=True)

    pipe = Pipeline()
    print(f"Mode: {pipe.extractor.mode.upper()}  (model: {config.ANTHROPIC_MODEL if pipe.extractor.mode == 'live' else 'mock payloads'})\n")
    for pdf in _iter_pdfs(args.targets):
        out = pipe.process_pdf(pdf)
        r = out.result
        print(f"{pdf.name:<34} PO={r.order.po_number or '—':<12} {r.status.value:<16} "
              f"flags={len(r.flags)} total=${r.order_total:,.2f}")
        for f in r.flags:
            print(f"    ⚠ {f.code.value}: {f.message}")
        if out.csv_path:
            print(f"    → {out.csv_path.relative_to(config.BASE_DIR)}")
