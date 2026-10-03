"""Staff, schedule, attendance, leave and commission schemas (Doc 10.6 — E1–E8)."""
from datetime import date, datetime, time
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

StaffRole = Literal["barber", "clerk"]
CommissionType = Literal["percentage", "fixed"]


class StaffCreateRequest(BaseModel):
    """E1 — the owner adds an employee. Owner-only by design (Doc 2.7)."""

    full_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., min_length=10, max_length=15)
    email: Optional[str] = Field(None, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    branch_id: str
    staff_role: StaffRole
    commission_type: CommissionType = "percentage"
    # percentage → 0-100; fixed → whole KES per sale. Validated below.
    commission_value: int = Field(..., ge=0, le=100_000)
    photo_url: Optional[str] = None

    @model_validator(mode="after")
    def _check_commission(self) -> "StaffCreateRequest":
        if self.commission_type == "percentage" and self.commission_value > 100:
            raise ValueError("percentage commission must be 0-100")
        return self


class StaffUpdateRequest(BaseModel):
    """E1 — the owner edits a profile. Changing a commission rate is allowed
    here but never retroactively: it applies to sales recorded from now on."""

    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    branch_id: Optional[str] = None
    staff_role: Optional[StaffRole] = None
    commission_type: Optional[CommissionType] = None
    commission_value: Optional[int] = Field(None, ge=0, le=100_000)
    photo_url: Optional[str] = None
    status: Optional[Literal["active", "suspended"]] = None


class StaffResponse(BaseModel):
    id: str
    business_id: str
    branch_id: str
    user_id: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    staff_role: str
    commission_type: str
    commission_value: int
    photo_url: Optional[str] = None
    status: str


# ── E5 — weekly schedule ─────────────────────────────────────────────────────
class ScheduleCreateRequest(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)  # 0=Mon … 6=Sun
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _check_order(self) -> "ScheduleCreateRequest":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class ScheduleResponse(BaseModel):
    id: str
    staff_id: str
    day_of_week: int
    start_time: str
    end_time: str


# ── E6 — attendance ──────────────────────────────────────────────────────────
class AttendanceResponse(BaseModel):
    id: str
    staff_id: str
    branch_id: str
    work_date: date
    check_in_at: Optional[datetime] = None
    check_out_at: Optional[datetime] = None


# ── E7 — leave ───────────────────────────────────────────────────────────────
class LeaveRequestCreateRequest(BaseModel):
    leave_type: Literal["annual", "sick", "other"]
    start_date: date
    end_date: date
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_dates(self) -> "LeaveRequestCreateRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class LeaveDecisionRequest(BaseModel):
    """E7 — the owner approves or rejects."""

    status: Literal["approved", "rejected"]


class LeaveRequestResponse(BaseModel):
    id: str
    staff_id: str
    staff_name: Optional[str] = None
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = None
    status: str
    decided_by: Optional[str] = None


# ── E2/E3/E4 — commissions ──────────────────────────────────────────────────
class CommissionResponse(BaseModel):
    id: str
    staff_id: str
    staff_name: Optional[str] = None
    branch_id: str
    period_start: date
    period_end: date
    earned_kes: int
    bonus_kes: int
    status: str


class BonusUpdateRequest(BaseModel):
    """E4 — owner sets an incentive bonus. Floored at 0 so it cannot be negative."""

    bonus_kes: int = Field(..., ge=0)
