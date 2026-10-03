"""Business / branch / service schemas (Doc 10.3 — B1–B6)."""

from datetime import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

BusinessType = Literal["barber_shop", "salon", "nail_bar", "beauty_clinic", "spa"]


# ── B1 — create business + first branch (onboarding screen 2) ───────────────
class BusinessCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    business_type: BusinessType
    county: Optional[str] = Field(None, max_length=80)
    town: Optional[str] = Field(None, max_length=80)
    logo_url: Optional[str] = None
    chat_enabled: bool = True

    # The first branch is created in the same call so the shop is usable
    # immediately after onboarding (OW1 — "set up in minutes").
    branch_name: str = Field(..., min_length=2, max_length=120)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    num_chairs: int = Field(1, ge=1, le=200)


class BranchResponse(BaseModel):
    id: str
    business_id: str
    # Public booking link segment, e.g. /public/kilimani-main/appointments
    slug: str
    name: str
    county: Optional[str] = None
    town: Optional[str] = None
    opens_at: Optional[str] = None
    closes_at: Optional[str] = None
    num_chairs: int
    is_active: bool


# ── B2 — business summary ────────────────────────────────────────────────────
class BusinessResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    business_type: BusinessType
    logo_url: Optional[str] = None
    county: Optional[str] = None
    town: Optional[str] = None
    status: str
    trial_ends_at: Optional[str] = None
    chat_enabled: bool
    plan: Optional[str] = None


class BusinessCreateResponse(BaseModel):
    business: BusinessResponse
    branch: BranchResponse
    next: str = "subscription"


# ── B3 — update business ─────────────────────────────────────────────────────
class BusinessUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=160)
    logo_url: Optional[str] = None
    chat_enabled: Optional[bool] = None


# ── B4/B5 — branches ─────────────────────────────────────────────────────────
class BranchCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    county: Optional[str] = Field(None, max_length=80)
    town: Optional[str] = Field(None, max_length=80)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    num_chairs: int = Field(1, ge=1, le=200)


class BranchUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    county: Optional[str] = Field(None, max_length=80)
    town: Optional[str] = Field(None, max_length=80)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    num_chairs: Optional[int] = Field(None, ge=1, le=200)
    is_active: Optional[bool] = None


# ── B6 — service catalogue ───────────────────────────────────────────────────
class ServiceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    price_kes: int = Field(..., ge=0)
    duration_min: int = Field(..., ge=5, le=480)
    is_active: bool = True


class ServiceUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    price_kes: Optional[int] = Field(None, ge=0)
    duration_min: Optional[int] = Field(None, ge=5, le=480)
    is_active: Optional[bool] = None


class ServiceResponse(BaseModel):
    id: str
    branch_id: str
    name: str
    price_kes: int
    duration_min: int
    is_active: bool
