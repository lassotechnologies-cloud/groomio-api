"""Chair schemas (Doc 10.6 — E8)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

ChairStatus = Literal["free", "occupied", "reserved", "offline"]


class ChairCreateRequest(BaseModel):
    chair_number: int = Field(..., ge=1, le=999)
    assigned_staff_id: Optional[str] = None
    status: ChairStatus = "free"


class ChairUpdateRequest(BaseModel):
    chair_number: Optional[int] = Field(None, ge=1, le=999)
    assigned_staff_id: Optional[str] = None
    status: Optional[ChairStatus] = None


class ChairResponse(BaseModel):
    id: str
    branch_id: str
    chair_number: int
    assigned_staff_id: Optional[str] = None
    assigned_staff_name: Optional[str] = None
    status: str
