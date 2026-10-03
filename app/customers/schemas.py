"""Customer schemas (Doc 10.4 — C1–C5)."""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.phone import normalize_phone


class CustomerCreateRequest(BaseModel):
    """C2 — create a profile. The clerk is entering someone walking in."""

    full_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=10, max_length=15)
    branch_id: str
    photo_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    birthday: Optional[date] = None

    # Canonical E.164 so search and duplicate detection match on one form.
    _phone = field_validator("phone")(normalize_phone)


class CustomerUpdateRequest(BaseModel):
    """C4 — preferences, notes, birthday. Name/phone change needs a merge flow."""

    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    photo_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    birthday: Optional[date] = None
    branch_id: Optional[str] = None


class CustomerResponse(BaseModel):
    id: str
    business_id: str
    branch_id: str
    full_name: str
    phone: str
    photo_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    loyalty_points: int
    birthday: Optional[str] = None
    created_at: Optional[str] = None


class CustomerListResponse(BaseModel):
    items: list[CustomerResponse]
    total: int
    page: int
    page_size: int


class VisitResponse(BaseModel):
    """One row in the visit history (C5) — either a booking or a recorded sale."""

    id: str
    kind: Literal["appointment", "sale"]
    scheduled_at: Optional[datetime] = None
    occurred_at: Optional[datetime] = None
    staff_name: Optional[str] = None
    status: str
    total_kes: int
    service_names: list[str] = []
