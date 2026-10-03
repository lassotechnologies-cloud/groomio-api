"""Appointment schemas (Doc 10.5 — D1–D4)."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

AppointmentStatus = Literal[
    "booked", "confirmed", "in_service", "completed", "no_show", "cancelled"
]
AppointmentSource = Literal["walk_in", "online", "staff"]


def _normalize_phone(v: str) -> str:
    digits = "".join(ch for ch in v if ch.isdigit())
    if digits.startswith("0"):
        digits = "+254" + digits[1:]
    elif digits.startswith("254"):
        digits = "+" + digits
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


class AppointmentCreateRequest(BaseModel):
    """D2 — staff or walk-in booking."""

    branch_id: str
    customer_id: str
    staff_id: str
    scheduled_at: datetime
    service_ids: list[str] = Field(..., min_length=1)
    chair_id: Optional[str] = None
    source: AppointmentSource = "staff"
    notes: Optional[str] = None

    # Priced from the service catalogue server-side; never trust a client price.


class PublicBookingRequest(BaseModel):
    """D3 — a customer booking themselves via a shared link. No auth."""

    customer_name: str = Field(..., min_length=2, max_length=120)
    customer_phone: str = Field(..., min_length=10, max_length=15)
    staff_id: str
    scheduled_at: datetime
    service_ids: list[str] = Field(..., min_length=1)
    notes: Optional[str] = None

    _phone = field_validator("customer_phone")(_normalize_phone)


class AppointmentUpdateStatusRequest(BaseModel):
    """D4 — move an appointment along its lifecycle."""

    status: AppointmentStatus
    notes: Optional[str] = None


class AppointmentLineResponse(BaseModel):
    service_id: str
    name: str
    price_kes: int
    duration_min: int


class AppointmentResponse(BaseModel):
    id: str
    business_id: str
    branch_id: str
    customer_id: str
    customer_name: Optional[str] = None
    staff_id: str
    staff_name: Optional[str] = None
    chair_id: Optional[str] = None
    scheduled_at: datetime
    duration_min: int
    source: str
    status: str
    notes: Optional[str] = None
    total_kes: int
    lines: list[AppointmentLineResponse] = []


class ConflictResponse(BaseModel):
    """What D2/D3 return on a clash — the next free slots, not just an error (CL3 ★).

    A barber double-booked should not be told "no" and left guessing; the owner
    gets actionable alternatives in the same response.
    """

    error: str = "slot_taken"
    message: str
    conflicting_appointment_id: str
    next_free_slots: list[datetime] = Field(..., min_length=1)


class QueueEntryCreateRequest(BaseModel):
    """D6 — add a walk-in to the live queue."""

    customer_id: str
    service_ids: list[str] = Field(default_factory=list)


class QueueEntryResponse(BaseModel):
    id: str
    branch_id: str
    customer_id: str
    customer_name: Optional[str] = None
    staff_id: Optional[str] = None
    chair_id: Optional[str] = None
    queue_number: int
    status: str
    joined_at: datetime
    served_at: Optional[datetime] = None
    est_wait_min: Optional[int] = None


class PublicQueuePositionResponse(BaseModel):
    """D8 — a waiting customer's own position. Scoped by a ref they were given."""

    queue_number: int
    status: str
    people_ahead: int
    est_wait_min: int
    joined_at: datetime
