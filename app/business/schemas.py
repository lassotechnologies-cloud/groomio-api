"""Business / branch / service schemas (Doc 10.3 — B1–B6)."""

from datetime import time
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

BusinessType = Literal["barber_shop", "salon", "nail_bar", "beauty_clinic", "spa"]


# ── B1 — create business + first branch (onboarding screen 2) ───────────────
class BusinessCreateRequest(BaseModel):
    """The whole of onboarding step 2, in a single call.

    `extra="ignore"` is stated rather than inherited. Ownership is derived from
    the caller's token, so a client that posts `owner_user_id` or `business_id`
    must have those keys dropped silently instead of bound to a model attribute
    that something might later read. Rejecting them with a 422 would be louder,
    but it would also fail an otherwise-valid submission: the field was never
    the client's to set, and an error naming it advertises a rule the API has
    no need to teach.

    Every field below already has a column. This task required no migration.
    """

    model_config = ConfigDict(extra="ignore")

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

    # Street address and public contact for the first branch. Both columns have
    # existed on `branches` since migration 0007 and the public booking portal
    # already renders them, but they were unreachable from this payload — an
    # owner had no way to publish the number a customer calls when they cannot
    # find the shop. Optional: a shop reachable only by its own booking link
    # needs neither, and discovery skips branches with no coordinates anyway.
    branch_address: Optional[str] = Field(None, max_length=200)
    # Stored as typed, deliberately not run through `normalize_phone`. That
    # helper is Kenyan-mobile-specific by design and rewrites a landline
    # ("020 123 4567") into something that dials the wrong number. A shop's
    # public line is very often a landline, and it is shown to a customer
    # rather than texted, so canonicalising it would corrupt it.
    branch_phone: Optional[str] = Field(None, max_length=20)

    @field_validator("branch_address", "branch_phone", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        """Treat an empty form field as "not supplied".

        An HTML form submits `""` for a field the owner left untouched, which is
        not the same as omitting the key. Without this the branch stores `""`
        and the public booking page renders an empty address line instead of
        hiding it, and `""` is a value every "is this set?" check has to learn
        to reject.
        """
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v


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
