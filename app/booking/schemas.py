"""Response shapes for the public booking portal (CU1–CU3).

These are deliberately *thin*: a customer can only ever read what a price list
on the wall already tells them. No customer records, no staff commission data,
no revenue. Keeping the public surface this narrow is what makes leaving these
routes unauthenticated defensible.
"""

from datetime import datetime, time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PublicBranchResponse(BaseModel):
    """The shop behind a shared link — CU1."""

    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    town: Optional[str] = None
    county: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_tier: Optional[int] = None
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    num_chairs: int
    business_name: str


class PublicServiceResponse(BaseModel):
    """A bookable service with its price and how long it takes."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    price_kes: int
    duration_min: int


class PublicStaffResponse(BaseModel):
    """A barber a customer can pick. `staff_id` is what booking submits."""

    staff_id: str
    full_name: str
    photo_url: Optional[str] = None


class PublicSlotResponse(BaseModel):
    """A bookable start time, already proven free by the conflict check."""

    scheduled_at: datetime
    duration_min: int
    total_kes: int
    # True when this is the earliest slot we would have offered had the customer
    # not pre-selected a different barber — lets the portal nudge them earlier.
    is_earliest: bool = False


class PublicSlotDayResponse(BaseModel):
    """All free slots for one barber on one day."""

    staff_id: str
    date: str  # ISO yyyy-mm-dd, so the client never parses a date
    duration_min: int
    total_kes: int
    slots: list[PublicSlotResponse] = Field(default_factory=list)
