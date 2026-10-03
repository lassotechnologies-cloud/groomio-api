"""Inventory schemas (Doc 10.7 — G1, G2)."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class ProductCreateRequest(BaseModel):
    """G1 — a retail item the counter can sell."""

    name: str = Field(..., min_length=1, max_length=120)
    category: Optional[str] = Field(None, max_length=80)
    cost_price_kes: int = Field(..., ge=0)
    sell_price_kes: int = Field(..., ge=0)
    stock_qty: int = Field(0, ge=0)
    low_stock_threshold: int = Field(5, ge=0)


class ProductUpdateRequest(BaseModel):
    """G1 — every field optional; only what is sent is touched."""

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    category: Optional[str] = Field(None, max_length=80)
    cost_price_kes: Optional[int] = Field(None, ge=0)
    sell_price_kes: Optional[int] = Field(None, ge=0)
    low_stock_threshold: Optional[int] = Field(None, ge=0)


class StockAdjustRequest(BaseModel):
    """G1 — restock, damage, or a stock count correction.

    `delta` rather than an absolute count: a shop that restocks 12 of something
    knows "12 arrived", not "we now have 37". Absolute counts lose that and make
    the audit trail a sequence of uninterpretable numbers.
    """

    delta: int
    reason: str = Field("restock", max_length=80)


class ProductResponse(BaseModel):
    id: str
    branch_id: str
    name: str
    category: Optional[str] = None
    cost_price_kes: int
    sell_price_kes: int
    stock_qty: int
    low_stock_threshold: int
    # Computed, not stored: the threshold alone is meaningless without the
    # comparison, and a client that forgot it would miss the restock entirely.
    is_low_stock: bool
    # Margin in whole KES. Owners ask for this more than they ask for revenue.
    margin_kes: int
    created_at: Optional[datetime] = None


class SupplierCreateRequest(BaseModel):
    """G2 — who the shop buys from."""

    name: str = Field(..., min_length=1, max_length=120)
    phone: Optional[str] = Field(None, max_length=15)


class SupplierUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    phone: Optional[str] = Field(None, max_length=15)


class SupplierResponse(BaseModel):
    id: str
    business_id: str
    name: str
    phone: Optional[str] = None
    last_order_at: Optional[date] = None
