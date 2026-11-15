"""Rules & reconciliation engine: extracted PO  x  ERP master data.

Matching strategy (per line, first hit wins)
  1. exact internal SKU            -> confidence 100
  2. exact legacy customer SKU     -> confidence 100
  3. normalised SKU (no dashes/spaces/case) on either column -> 98
  4. fuzzy description (rapidfuzz WRatio) over the whole catalog

Business rules that raise flags (any flag => REQUIRES_REVIEW)
  * PO number missing / already imported
  * customer not found or ambiguous (< threshold or two close candidates)
  * ship-to differs from the ERP address on file
  * item not found / ambiguous (confidence < 85 % or two close candidates)
  * unit price differs from the ERP price list by more than 1 %
  * non-positive quantity
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process, utils

import config
import database
from models import (Flag, FlagCode, LineItem, MatchedLine, PurchaseOrder, Status,
                    ValidationResult)

SHIP_TO_THRESHOLD = 80.0
NOT_FOUND_FLOOR = 60.0  # below this we don't even suggest candidates


def _norm_sku(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _norm_text(s: str) -> str:
    """Normalise units so '40 mm', '40mm', 'M8x40' compare well."""
    s = utils.default_process(s)
    s = re.sub(r"(\d)\s*(mm|in|ft|yd|ct|ga)\b", r"\1\2", s)
    s = re.sub(r"(\d)\s*x\s*(\d)", r"\1 x \2", s)
    s = re.sub(r"\bm(\d+)x(\d+)", r"m\1 x \2", s)
    return s


class POValidator:
    def __init__(self, db_path: Optional[Path] = None,
                 price_tolerance: float = config.PRICE_TOLERANCE,
                 confidence_threshold: float = config.MATCH_CONFIDENCE_THRESHOLD,
                 ambiguity_margin: float = config.AMBIGUITY_MARGIN):
        self.db_path = db_path
        self.price_tolerance = price_tolerance
        self.threshold = confidence_threshold
        self.margin = ambiguity_margin
        self.customers = database.fetch_customers(db_path)
        self.catalog = database.fetch_catalog(db_path)
        self._by_sku = {c["internal_sku"]: c for c in self.catalog}
        self._by_legacy = {c["legacy_customer_sku"]: c for c in self.catalog if c["legacy_customer_sku"]}
        self._by_norm = {}
        for c in self.catalog:
            self._by_norm[_norm_sku(c["internal_sku"])] = c
            if c["legacy_customer_sku"]:
                self._by_norm[_norm_sku(c["legacy_customer_sku"])] = c
        self._desc_choices = {c["internal_sku"]: _norm_text(c["description"]) for c in self.catalog}

    # ------------------------------------------------------------------ API
    def validate(self, order: PurchaseOrder, order_id: Optional[int] = None) -> ValidationResult:
        flags: list[Flag] = []

        # --- header rules
        if not order.po_number:
            flags.append(Flag(code=FlagCode.MISSING_PO_NUMBER, field="PO_Number",
                              message="PO number is missing on the document."))
        elif database.po_exists(order.po_number, exclude_id=order_id, db_path=self.db_path):
            flags.append(Flag(code=FlagCode.DUPLICATE_PO, field="PO_Number",
                              message=f"PO {order.po_number} was already approved and exported."))

        customer, cust_conf, cust_flags = self._match_customer(order)
        flags += cust_flags

        # --- lines
        lines: list[MatchedLine] = []
        for idx, item in enumerate(order.line_items, start=1):
            line, line_flags = self._match_line(idx, item)
            lines.append(line)
            flags += line_flags

        return ValidationResult(
            status=Status.REQUIRES_REVIEW if flags else Status.APPROVED,
            order=order,
            customer_id=customer["customer_id"] if customer else None,
            erp_customer_name=customer["name"] if customer else None,
            erp_ship_to=customer["ship_to_address"] if customer else None,
            customer_confidence=round(cust_conf, 1),
            lines=lines,
            flags=flags,
        )

    # -------------------------------------------------------------- customer
    def _match_customer(self, order: PurchaseOrder):
        flags: list[Flag] = []
        names = {c["customer_id"]: c["name"] for c in self.customers}
        hits = process.extract(order.customer_name, names, scorer=fuzz.WRatio,
                               processor=utils.default_process, limit=2)
        if not hits:
            return None, 0.0, [Flag(code=FlagCode.CUSTOMER_NOT_FOUND, field="Customer_Name",
                                    message="Customer master data is empty.")]
        (_, best_score, best_id), *rest = hits
        second_score = rest[0][1] if rest else 0.0
        best = next(c for c in self.customers if c["customer_id"] == best_id)

        if best_score < self.threshold:
            code = FlagCode.CUSTOMER_AMBIGUOUS if best_score >= NOT_FOUND_FLOOR else FlagCode.CUSTOMER_NOT_FOUND
            flags.append(Flag(code=code, field="Customer_Name",
                              message=f"'{order.customer_name}' best match is '{best['name']}' "
                                      f"({best_score:.0f}% < {self.threshold:.0f}%)."))
            return (best if code == FlagCode.CUSTOMER_AMBIGUOUS else None), best_score, flags
        if best_score - second_score < self.margin and best_score < 100:
            other = names[rest[0][2]]
            flags.append(Flag(code=FlagCode.CUSTOMER_AMBIGUOUS, field="Customer_Name",
                              message=f"'{order.customer_name}' matches both '{best['name']}' "
                                      f"({best_score:.0f}%) and '{other}' ({second_score:.0f}%)."))

        # ship-to cross-check also helps disambiguate similar customer names
        if order.ship_to:
            st_score = fuzz.token_set_ratio(order.ship_to, best["ship_to_address"],
                                            processor=utils.default_process)
            if st_score < SHIP_TO_THRESHOLD:
                flags.append(Flag(code=FlagCode.SHIP_TO_MISMATCH, field="Ship_To",
                                  message=f"Ship-to differs from ERP address on file "
                                          f"'{best['ship_to_address']}' ({st_score:.0f}% similar)."))
        return best, best_score, flags

    # ------------------------------------------------------------------ line
    def _match_line(self, line_no: int, item: LineItem):
        flags: list[Flag] = []
        line = MatchedLine(line_no=line_no, source=item)

        erp, method, conf = None, "none", 0.0
        if item.sku and item.sku in self._by_sku:
            erp, method, conf = self._by_sku[item.sku], "exact_sku", 100.0
        elif item.sku and item.sku in self._by_legacy:
            erp, method, conf = self._by_legacy[item.sku], "legacy_sku", 100.0
        elif item.sku and _norm_sku(item.sku) in self._by_norm:
            erp, method, conf = self._by_norm[_norm_sku(item.sku)], "normalised_sku", 98.0
        else:
            hits = process.extract(_norm_text(item.description), self._desc_choices,
                                   scorer=fuzz.WRatio, processor=None, limit=3)
            line.candidates = [
                {"internal_sku": sku, "description": self._by_sku[sku]["description"],
                 "unit_price": self._by_sku[sku]["unit_price"], "score": round(score, 1)}
                for _, score, sku in hits
            ]
            if hits:
                _, conf, best_sku = hits[0]
                second = hits[1][1] if len(hits) > 1 else 0.0
                method = "fuzzy_description"
                if conf < NOT_FOUND_FLOOR:
                    flags.append(Flag(code=FlagCode.ITEM_NOT_FOUND, line_no=line_no, field="SKU",
                                      message=f"Line {line_no}: '{item.description}' not found in catalog."))
                else:
                    erp = self._by_sku[best_sku]
                    if conf < self.threshold:
                        flags.append(Flag(code=FlagCode.ITEM_AMBIGUOUS, line_no=line_no, field="SKU",
                                          message=f"Line {line_no}: low-confidence match '{erp['description']}' "
                                                  f"({conf:.0f}% < {self.threshold:.0f}%)."))
                    elif conf - second < self.margin:
                        alt = self._by_sku[hits[1][2]]["description"]
                        flags.append(Flag(code=FlagCode.ITEM_AMBIGUOUS, line_no=line_no, field="SKU",
                                          message=f"Line {line_no}: '{item.description}' is ambiguous between "
                                                  f"'{erp['description']}' ({conf:.0f}%) and '{alt}' ({second:.0f}%)."))

        line.match_method = method
        line.confidence = round(conf, 1)
        if erp:
            line.internal_sku = erp["internal_sku"]
            line.erp_description = erp["description"]
            line.erp_uom = erp["uom"]
            line.erp_unit_price = erp["unit_price"]
            variance = (item.unit_price - erp["unit_price"]) / erp["unit_price"]
            line.price_variance_pct = round(variance * 100, 2)
            if abs(variance) > self.price_tolerance:
                flags.append(Flag(code=FlagCode.PRICE_MISMATCH, line_no=line_no, field="Unit_Price",
                                  message=f"Line {line_no}: PO price {item.unit_price:,.2f} vs ERP "
                                          f"{erp['unit_price']:,.2f} ({variance:+.2%}, tolerance "
                                          f"±{self.price_tolerance:.0%})."))
        if item.qty <= 0:
            flags.append(Flag(code=FlagCode.INVALID_QTY, line_no=line_no, field="Qty",
                              message=f"Line {line_no}: quantity must be positive (got {item.qty})."))
        return line, flags
