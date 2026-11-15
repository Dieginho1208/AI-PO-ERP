"""Pydantic data contracts shared by the extractor, validator and UI.

The LLM must return JSON that matches ``PurchaseOrder`` *exactly*; field
aliases (``PO_Number``, ``Line_Items`` ...) are the wire format, snake_case
attributes are used in Python.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sku: Optional[str] = Field(None, alias="SKU", description="SKU exactly as printed on the PO (customer or vendor code). null if absent.")
    description: str = Field(..., alias="Description", description="Item description as printed.")
    qty: float = Field(..., alias="Qty", description="Ordered quantity.")
    uom: Optional[str] = Field(None, alias="UOM", description="Unit of measure if printed (EA, BX, CS, RL...).")
    unit_price: float = Field(..., alias="Unit_Price", description="Unit price as printed, numeric, no currency symbol.")

    @field_validator("sku", "uom", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    po_number: Optional[str] = Field(None, alias="PO_Number", description="Purchase order number. null if not present.")
    customer_name: str = Field(..., alias="Customer_Name", description="Buying company name (bill-to / buyer).")
    ship_to: Optional[str] = Field(None, alias="Ship_To", description="Full ship-to address on a single line.")
    line_items: list[LineItem] = Field(..., alias="Line_Items", min_length=1)

    @field_validator("po_number", mode="before")
    @classmethod
    def _blank_po(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    def to_wire(self) -> dict:
        return self.model_dump(by_alias=True)


class Status(str, Enum):
    APPROVED = "APPROVED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class FlagCode(str, Enum):
    MISSING_PO_NUMBER = "MISSING_PO_NUMBER"
    DUPLICATE_PO = "DUPLICATE_PO"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    CUSTOMER_AMBIGUOUS = "CUSTOMER_AMBIGUOUS"
    SHIP_TO_MISMATCH = "SHIP_TO_MISMATCH"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    ITEM_AMBIGUOUS = "ITEM_AMBIGUOUS"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    INVALID_QTY = "INVALID_QTY"


class Flag(BaseModel):
    code: FlagCode
    message: str
    line_no: Optional[int] = None  # 1-based; None = header-level
    field: Optional[str] = None


class MatchedLine(BaseModel):
    line_no: int
    source: LineItem
    internal_sku: Optional[str] = None
    erp_description: Optional[str] = None
    erp_uom: Optional[str] = None
    erp_unit_price: Optional[float] = None
    match_method: str = "none"  # exact_sku | legacy_sku | fuzzy_description | none
    confidence: float = 0.0
    candidates: list[dict] = Field(default_factory=list)
    price_variance_pct: Optional[float] = None


class ValidationResult(BaseModel):
    status: Status
    order: PurchaseOrder
    customer_id: Optional[str] = None
    erp_customer_name: Optional[str] = None
    erp_ship_to: Optional[str] = None
    customer_confidence: float = 0.0
    lines: list[MatchedLine]
    flags: list[Flag] = Field(default_factory=list)

    @property
    def order_total(self) -> float:
        return round(sum(l.source.qty * l.source.unit_price for l in self.lines), 2)
