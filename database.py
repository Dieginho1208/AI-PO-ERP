"""Mock ERP master data (SQLite) + persistence for processed orders.

Run ``python database.py --reset`` to (re)create the database with seed data.

The catalog intentionally contains look-alike items (zinc vs. stainless bolts,
box vs. case gloves, 2" vs. 3" tape...) so the fuzzy matcher has realistic
ambiguity to deal with.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    ship_to_address  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_items (
    internal_sku     TEXT PRIMARY KEY,
    legacy_customer_sku TEXT,           -- old SKU some customers still print on POs
    description      TEXT NOT NULL,
    uom              TEXT NOT NULL,
    unit_price       REAL NOT NULL      -- official ERP price list
);
CREATE INDEX IF NOT EXISTS idx_catalog_legacy ON catalog_items(legacy_customer_sku);

CREATE TABLE IF NOT EXISTS processed_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT NOT NULL,
    po_number       TEXT,
    customer_id     TEXT,
    customer_name   TEXT,
    status          TEXT NOT NULL,
    flag_count      INTEGER NOT NULL DEFAULT 0,
    order_total     REAL NOT NULL DEFAULT 0,
    extraction_mode TEXT NOT NULL,      -- live | demo
    result_json     TEXT NOT NULL,
    csv_path        TEXT,
    reviewed_by     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

CUSTOMERS = [
    ("C-1001", "Acme Industrial Supply Co.", "1200 Harbor Blvd, Dock 4, Long Beach, CA 90813, USA"),
    ("C-1002", "Northwind Logistics LLC", "88 Freight Way, Building B, Memphis, TN 38118, USA"),
    ("C-1003", "Globex Manufacturing Inc.", "4500 Industrial Pkwy, Columbus, OH 43219, USA"),
    ("C-1004", "Initech Facilities Group", "300 Corporate Dr, Suite 120, Austin, TX 78741, USA"),
    # Deliberately similar to C-1001 to stress customer matching
    ("C-1005", "Acme Industrial Services Ltd.", "77 Mill Road, Unit 9, Hamilton, ON L8N 3T2, Canada"),
    ("C-1006", "Umbrella Health Systems", "19 Raccoon St, Receiving, Denver, CO 80204, USA"),
]

CATALOG = [
    # internal_sku, legacy_customer_sku, description, uom, unit_price
    ("FAS-M8-40-ZN", "AC-55812", "Hex Bolt M8 x 40mm Zinc Plated", "BX100", 18.50),
    ("FAS-M8-40-SS", "AC-55813", "Hex Bolt M8 x 40mm Stainless Steel A2", "BX100", 34.90),
    ("FAS-M8-NUT-ZN", "AC-55820", "Hex Nut M8 Zinc Plated", "BX100", 6.75),
    ("FAS-WSH-M8", None, "Flat Washer M8 Zinc", "BX100", 4.20),
    ("PPE-GLV-NIT-L-100", "NW-GL-L", "Nitrile Gloves Large Powder-Free Box/100", "BX", 12.40),
    ("PPE-GLV-NIT-L-1000", "NW-GL-L-CS", "Nitrile Gloves Large Powder-Free Case/1000", "CS", 109.00),
    ("PPE-GLV-NIT-M-100", "NW-GL-M", "Nitrile Gloves Medium Powder-Free Box/100", "BX", 12.40),
    ("PPE-GOG-CLR", None, "Safety Goggles Anti-Fog Clear Lens", "EA", 7.95),
    ("PKG-TAPE-2-CLR", "PK-200", "Packing Tape 2in x 110yd Clear", "RL", 2.15),
    ("PKG-TAPE-3-CLR", "PK-300", "Packing Tape 3in x 110yd Clear", "RL", 3.05),
    ("PKG-BOX-1212", None, "Corrugated Box 12x12x12 32ECT", "EA", 1.38),
    ("PKG-STRETCH-18", "PK-SW18", "Stretch Wrap Film 18in x 1500ft 80ga", "RL", 21.60),
    ("ELE-CBL-C6-1000", None, "Cat6 Ethernet Cable UTP Blue 1000ft", "BX", 149.00),
    ("ELE-CBL-C6A-1000", None, "Cat6A Ethernet Cable UTP Blue 1000ft", "BX", 219.00),
    ("JAN-TOWEL-MF", None, "Multifold Paper Towels White 4000ct", "CS", 38.25),
    ("JAN-CLN-GP-1G", None, "General Purpose Cleaner Concentrate 1 Gallon", "EA", 14.80),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(reset: bool = False, db_path: Optional[Path] = None) -> None:
    """Create tables and seed master data (idempotent)."""
    path = Path(db_path or config.DB_PATH)
    if reset and path.exists():
        path.unlink()
    with get_conn(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?)", CUSTOMERS)
        conn.executemany("INSERT OR IGNORE INTO catalog_items VALUES (?,?,?,?,?)", CATALOG)


# --------------------------------------------------------------------- reads
def fetch_customers(db_path: Optional[Path] = None) -> list[dict]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM customers")]


def fetch_catalog(db_path: Optional[Path] = None) -> list[dict]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM catalog_items")]


def po_exists(po_number: str, exclude_id: Optional[int] = None, db_path: Optional[Path] = None) -> bool:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_orders WHERE po_number = ? AND status = 'APPROVED' AND id != ?",
            (po_number, exclude_id or -1),
        ).fetchone()
        return row is not None


# ------------------------------------------------------- processed orders
def save_order(source_file: str, result: dict, extraction_mode: str,
               csv_path: Optional[str] = None, order_id: Optional[int] = None,
               reviewed_by: Optional[str] = None, db_path: Optional[Path] = None) -> int:
    """Insert or update a processed order. ``result`` is ValidationResult.model_dump(mode='json')."""
    order = result["order"]
    total = round(sum(li["qty"] * li["unit_price"] for li in order["line_items"]), 2)
    values = (
        source_file, order.get("po_number"), result.get("customer_id"),
        result.get("erp_customer_name") or order.get("customer_name"),
        result["status"], len(result.get("flags", [])), total, extraction_mode,
        json.dumps(result), csv_path, reviewed_by,
    )
    with get_conn(db_path) as conn:
        if order_id is None:
            cur = conn.execute(
                """INSERT INTO processed_orders (source_file, po_number, customer_id, customer_name,
                   status, flag_count, order_total, extraction_mode, result_json, csv_path,
                   reviewed_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values + (_now(), _now()),
            )
            return int(cur.lastrowid)
        conn.execute(
            """UPDATE processed_orders SET source_file=?, po_number=?, customer_id=?, customer_name=?,
               status=?, flag_count=?, order_total=?, extraction_mode=?, result_json=?, csv_path=?,
               reviewed_by=?, updated_at=? WHERE id=?""",
            values + (_now(), order_id),
        )
        return order_id


def list_orders(status: Optional[str] = None, db_path: Optional[Path] = None) -> list[dict]:
    sql = "SELECT * FROM processed_orders"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY id DESC"
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["result"] = json.loads(r.pop("result_json"))
    return rows


def clear_orders(db_path: Optional[Path] = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM processed_orders")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Initialise the mock ERP database")
    ap.add_argument("--reset", action="store_true", help="drop and recreate the database")
    args = ap.parse_args()
    init_db(reset=args.reset)
    print(f"ERP database ready at {config.DB_PATH}")
    print(f"  customers:     {len(fetch_customers())}")
    print(f"  catalog_items: {len(fetch_catalog())}")
