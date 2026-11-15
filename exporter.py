"""ERP import file generation for APPROVED orders."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from models import Status, ValidationResult

CSV_COLUMNS = [
    "po_number", "customer_id", "customer_name", "ship_to", "line_no", "internal_sku",
    "description", "uom", "qty", "unit_price", "line_total", "match_method", "match_confidence",
]


def export_csv(result: ValidationResult, out_dir: Optional[Path] = None,
               operator_override: bool = False) -> Path:
    """Write one ERP import CSV per approved PO and return its path."""
    if result.status != Status.APPROVED and not operator_override:
        raise ValueError("Only APPROVED orders can be exported (or pass operator_override=True).")
    out_dir = Path(out_dir or config.ERP_IMPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    po = result.order.po_number or "NO-PO"
    safe_po = re.sub(r"[^A-Za-z0-9_-]", "_", po)
    path = out_dir / f"PO_{safe_po}_{datetime.now():%Y%m%d_%H%M%S}.csv"

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for line in result.lines:
            src = line.source
            writer.writerow({
                "po_number": po,
                "customer_id": result.customer_id or "",
                "customer_name": result.erp_customer_name or result.order.customer_name,
                "ship_to": result.order.ship_to or result.erp_ship_to or "",
                "line_no": line.line_no,
                "internal_sku": line.internal_sku or src.sku or "",
                "description": line.erp_description or src.description,
                "uom": line.erp_uom or src.uom or "",
                "qty": src.qty,
                "unit_price": f"{src.unit_price:.2f}",
                "line_total": f"{src.qty * src.unit_price:.2f}",
                "match_method": line.match_method,
                "match_confidence": line.confidence,
            })
    return path
