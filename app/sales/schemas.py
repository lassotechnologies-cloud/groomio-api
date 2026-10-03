"""Sale, expense and report schemas (Doc 10.7 — F1–F6)."""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

PaymentMethod = Literal["cash", "mpesa", "pesapal"]


class SaleLineRequest(BaseModel):
    """One line on a sale. Services and products are priced server-side."""

    item_type: Literal["service", "product"]
    # Exactly one of these, per the item_type above.
    service_id: Optional[str] = None
    product_id: Optional[str] = None
    quantity: int = Field(1, ge=1, le=999)

    @model_validator(mode="after")
    def _check_ref(self) -> "SaleLineRequest":
        needed = "service_id" if self.item_type == "service" else "product_id"
        other = "product_id" if self.item_type == "service" else "service_id"
        if not getattr(self, needed):
            raise ValueError(f"{needed} is required for a {self.item_type} line")
        if getattr(self, other):
            raise ValueError(f"{other} must not be set on a {self.item_type} line")
        return self


class SaleCreateRequest(BaseModel):
    """F1 — CL4: record a sale and take payment.

    One call that fans out into commission, stock decrement and loyalty points
    (Doc 10.7). Client-supplied prices are deliberately absent: a client could
    under-report a sale to dodge commission, so every price is read from the
    catalogue here.
    """

    branch_id: str
    staff_id: str
    lines: list[SaleLineRequest] = Field(..., min_length=1)
    customer_id: Optional[str] = None
    payment_method: PaymentMethod = "cash"
    # Optional explicit total; when present it must match the computed total, so
    # a mis-keyed amount is caught rather than silently recorded.
    expected_total_kes: Optional[int] = Field(None, ge=0)


class SaleLineResponse(BaseModel):
    id: str
    item_type: str
    name: str
    price_kes: int
    quantity: int
    commission_kes: int
    service_id: Optional[str] = None
    product_id: Optional[str] = None


class SaleResponse(BaseModel):
    id: str
    business_id: str
    branch_id: str
    customer_id: Optional[str] = None
    staff_id: str
    total_kes: int
    payment_method: str
    created_at: Optional[datetime] = None
    lines: list[SaleLineResponse] = []


class SaleFanoutResponse(BaseModel):
    """What the single sale write triggered, so the clerk sees it happen (CL4)."""

    sale: SaleResponse
    commission_kes: int
    loyalty_points_awarded: int
    products_decremented: list[str] = []
    low_stock_alerts: list[str] = []


# ── F3 — expenses ────────────────────────────────────────────────────────────
ExpenseCategory = Literal["rent", "stock", "utilities", "salaries", "other"]


class ExpenseCreateRequest(BaseModel):
    branch_id: str
    category: ExpenseCategory
    amount_kes: int = Field(..., ge=1)
    note: Optional[str] = Field(None, max_length=255)
    incurred_at: Optional[date] = None  # defaults to today


class ExpenseResponse(BaseModel):
    id: str
    branch_id: str
    category: str
    amount_kes: int
    note: Optional[str] = None
    incurred_at: date
    recorded_by: str


# ── F4/F5 — reports ──────────────────────────────────────────────────────────
class BarberPerformance(BaseModel):
    staff_id: str
    staff_name: Optional[str] = None
    sales_count: int
    revenue_kes: int
    commission_kes: int


class DailyReportResponse(BaseModel):
    """F4 — OW2: the owner's one-tap day view."""

    branch_id: Optional[str] = None
    on_date: date
    customers_served: int
    sales_count: int
    revenue_kes: int
    expenses_kes: int
    profit_kes: int
    top_barber: Optional[BarberPerformance] = None
    barbers: list[BarberPerformance] = []


class PeriodReportResponse(BaseModel):
    """F5 — aggregates plus a per-branch comparison."""

    period: str
    branch_id: Optional[str] = None
    from_date: date
    to_date: date
    customers_served: int
    sales_count: int
    revenue_kes: int
    expenses_kes: int
    profit_kes: int
    average_sale_kes: int
    barbers: list[BarberPerformance] = []


class BranchComparison(BaseModel):
    branch_id: str
    branch_name: str
    sales_count: int
    revenue_kes: int
    expenses_kes: int
    profit_kes: int
