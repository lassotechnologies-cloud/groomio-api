"""Chair routes — API group E8 (Doc 10.6).

Chairs are the shop's real capacity. Queue wait estimates divide by them, so a
branch that has four barbers but two chairs must not promise four-way throughput.
Marking a chair `offline` removes it from that capacity without deleting history.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chairs.schemas import ChairCreateRequest, ChairResponse, ChairUpdateRequest
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.tenancy import resolve_business_id
from app.models.identity import User
from app.models.operations import Chair
from app.models.people import Staff
from app.models.tenant import Branch

router = APIRouter(tags=["chairs"])

Db = Annotated[AsyncSession, Depends(get_db)]
OwnerOrClerk = Depends(require_role("owner", "clerk"))


async def _load_branch(db: AsyncSession, business_id: str, branch_id: str) -> Branch:
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch


def _out(chair: Chair, names: dict[str, str]) -> ChairResponse:
    return ChairResponse(
        id=str(chair.id),
        branch_id=chair.branch_id,
        chair_number=chair.chair_number,
        assigned_staff_id=chair.assigned_staff_id,
        assigned_staff_name=names.get(chair.assigned_staff_id),
        status=chair.status,
    )


async def _assigned_names(db: AsyncSession, staff_ids: set[str]) -> dict[str, str]:
    if not staff_ids:
        return {}
    stmt = (
        select(Staff.id, User.full_name)
        .join(User, User.id == Staff.user_id)
        .where(Staff.id.in_(staff_ids))
    )
    return {str(sid): name for sid, name in (await db.execute(stmt)).all()}


@router.get(
    "/branches/{branch_id}/chairs",
    response_model=list[ChairResponse],
    dependencies=[OwnerOrClerk],
)
async def list_chairs(
    branch_id: str,
    db: Db,
    user: CurrentUser,
    include_offline: bool = Query(True, description="Hide broken chairs when false"),
) -> list[ChairResponse]:
    """The branch's chairs in physical order."""
    business_id = await resolve_business_id(db, user)
    await _load_branch(db, business_id, branch_id)

    stmt = select(Chair).where(Chair.branch_id == branch_id)
    if not include_offline:
        stmt = stmt.where(Chair.status != "offline")

    rows = list(await db.scalars(stmt.order_by(Chair.chair_number)))
    names = await _assigned_names(
        db, {c.assigned_staff_id for c in rows if c.assigned_staff_id}
    )
    return [_out(c, names) for c in rows]


@router.post(
    "/branches/{branch_id}/chairs",
    response_model=ChairResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[OwnerOrClerk],
)
async def create_chair(
    branch_id: str, payload: ChairCreateRequest, db: Db, user: CurrentUser
) -> ChairResponse:
    """Add a chair. Numbers are unique per branch so the shop floor is unambiguous."""
    business_id = await resolve_business_id(db, user)
    await _load_branch(db, business_id, branch_id)

    clash = await db.scalar(
        select(func.count())
        .select_from(Chair)
        .where(Chair.branch_id == branch_id, Chair.chair_number == payload.chair_number)
    )
    if clash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Chair number {payload.chair_number} already exists",
        )

    if payload.assigned_staff_id:
        staff = await db.scalar(
            select(Staff).where(
                Staff.id == payload.assigned_staff_id,
                Staff.business_id == business_id,
                Staff.branch_id == branch_id,
            )
        )
        if not staff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Staff not in this branch"
            )

    chair = Chair(
        branch_id=branch_id,
        chair_number=payload.chair_number,
        assigned_staff_id=payload.assigned_staff_id,
        status=payload.status,
    )
    db.add(chair)
    await db.flush()

    names = await _assigned_names(
        db, {chair.assigned_staff_id} if chair.assigned_staff_id else set()
    )
    return _out(chair, names)


@router.patch(
    "/branches/{branch_id}/chairs/{chair_id}",
    response_model=ChairResponse,
    dependencies=[OwnerOrClerk],
)
async def update_chair(
    branch_id: str,
    chair_id: str,
    payload: ChairUpdateRequest,
    db: Db,
    user: CurrentUser,
) -> ChairResponse:
    """Reassign or take a chair offline. The queue endpoint maintains `occupied`."""
    business_id = await resolve_business_id(db, user)
    await _load_branch(db, business_id, branch_id)

    chair = await db.scalar(
        select(Chair).where(Chair.id == chair_id, Chair.branch_id == branch_id)
    )
    if not chair:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if payload.chair_number is not None and payload.chair_number != chair.chair_number:
        clash = await db.scalar(
            select(func.count())
            .select_from(Chair)
            .where(
                Chair.branch_id == branch_id,
                Chair.chair_number == payload.chair_number,
                Chair.id != chair_id,
            )
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Chair number {payload.chair_number} already exists",
            )
        chair.chair_number = payload.chair_number

    if payload.assigned_staff_id is not None:
        staff = await db.scalar(
            select(Staff).where(
                Staff.id == payload.assigned_staff_id,
                Staff.business_id == business_id,
                Staff.branch_id == branch_id,
            )
        )
        if not staff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Staff not in this branch"
            )
        chair.assigned_staff_id = payload.assigned_staff_id or None

    if payload.status is not None:
        # A chair that is physically occupied cannot be silently marked offline
        # while someone is in it.
        if payload.status == "offline" and chair.status == "occupied":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Free the chair before taking it offline",
            )
        chair.status = payload.status

    names = await _assigned_names(
        db, {chair.assigned_staff_id} if chair.assigned_staff_id else set()
    )
    return _out(chair, names)
