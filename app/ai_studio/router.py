"""AI Marketing Studio routes — API group I7, I8, I9 (Doc 10.11).

An owner uploads their shop logo and photos once, and gets back captions and
visuals they can post on Facebook, Instagram, WhatsApp and TikTok without hiring
anyone. That is the whole pitch of the studio, and the pitch only works if it
produces something in a reasonable time.

So generation is never inline. I8 returns `202 Accepted` and hands the work to
Celery; composing a slideshow video takes far longer than a request on a Kenyan
mobile connection will last, and an owner staring at a spinner will tap generate
four times, which is how you end up with duplicate assets.

`GET /ai/projects/{id}` is the polling endpoint the client hits while the worker
runs, and it is the only place assets are read from — assets are created by the
worker, never by the client, so there is no endpoint through which a fabricated
poster could be inserted.

Access is owner-only. A barber asking the studio to make a poster for the shop
is not a use case worth the abuse surface (Doc 4.2 — marketing spend is the
owner's decision).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_studio.schemas import (
    AIAssetResponse,
    AIGenerateResponse,
    AIProjectCreateRequest,
    AIProjectResponse,
    MIN_PHOTOS,
)
from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.storage import sign_reads
from app.core.tenancy import resolve_business_id
from app.models.ai import AIAsset, AIProject

router = APIRouter(tags=["ai_studio"])

Db = Annotated[AsyncSession, Depends(get_db)]
Owner = Depends(require_role("owner"))


async def _project(db: AsyncSession, business_id: str, project_id: str) -> AIProject:
    """Fetch a project inside the caller's tenant. 404 on cross-tenant (Doc 4.3)."""
    project = await db.scalar(
        select(AIProject).where(
            AIProject.id == project_id, AIProject.business_id == business_id
        )
    )
    if not project:
        # Not 403: telling a shop that another shop's project exists is a leak.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


async def _assets_for(db: AsyncSession, project_id: str) -> list[AIAsset]:
    """A project's assets, oldest first so captions and posters stay in order."""
    return list(
        (
            await db.scalars(
                select(AIAsset)
                .where(AIAsset.project_id == project_id)
                .order_by(AIAsset.created_at)
            )
        ).all()
    )


def _signed(asset: AIAsset, signed: dict[str, str]) -> str | None:
    """The asset's viewable URL, or its key when R2 is not configured.

    Rows store the R2 *key*, never a signed URL: a signed link expires in minutes,
    so a row holding one shows a broken image tomorrow. The key is only useful
    with a fresh signature, which is why signing happens here, on read.
    """
    if not asset.file_url:
        return None
    return signed.get(asset.file_url, asset.file_url)


def _sign_assets(assets: list[AIAsset]) -> list[AIAssetResponse]:
    """Sign every file-backed asset in one batch.

    Signatures are fetched per project rather than per asset, because boto3
    signs locally with no network call but building a client per asset is the
    kind of thing that makes a list endpoint slow for no reason.
    """
    keys = [a.file_url for a in assets if a.file_url]
    signed = sign_reads(keys) if keys else {}
    return [
        AIAssetResponse(
            id=str(a.id),
            asset_type=a.asset_type,
            content_text=a.content_text,
            file_url=_signed(a, signed),
            created_at=a.created_at,
        )
        for a in assets
    ]


def _project_out(
    project: AIProject, assets: list[AIAsset], pending_photos: int
) -> AIProjectResponse:
    return AIProjectResponse(
        id=str(project.id),
        business_id=project.business_id,
        shop_name=project.shop_name,
        logo_url=project.logo_url,
        photo_count=len(project.photo_urls or []),
        status=project.status,
        photos_needed=max(0, MIN_PHOTOS - len(project.photo_urls or [])),
        assets=_sign_assets(assets),
        created_at=project.created_at,
    )


# ── I7 — create a project ───────────────────────────────────────────────────
@router.post(
    "/ai/projects",
    response_model=AIProjectResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Owner],
)
async def create_project(
    payload: AIProjectCreateRequest, db: Db, user: CurrentUser
) -> AIProjectResponse:
    """Register a project's source material. Accepts one or more photos; the
    minimum of five is enforced at generation (I8) rather than here, so an owner
    part-way through uploading is not blocked from saving their work.
    """
    business_id = await resolve_business_id(db, user)

    project = AIProject(
        business_id=business_id,
        shop_name=payload.shop_name,
        logo_url=payload.logo_url,
        photo_urls=payload.photo_urls,
        status="uploaded",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    return _project_out(project, [], len(payload.photo_urls))


# ── I8 — kick off generation ─────────────────────────────────────────────────
@router.post(
    "/ai/projects/{project_id}/generate",
    response_model=AIGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Owner],
)
async def generate_project(
    project_id: str, db: Db, user: CurrentUser
) -> AIGenerateResponse:
    """Queue asset generation. Returns 202 — poll I9 for results.

    Re-triggering a `processing` project is rejected rather than queued again.
    The owner tapping the button twice is the expected case here, and the worker's
    idempotency guard on `ready` does not cover a job still in flight.
    """
    business_id = await resolve_business_id(db, user)
    project = await _project(db, business_id, project_id)

    if project.status == "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Assets already generated for this project",
        )
    if project.status == "processing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generation already in progress",
        )

    photos = project.photo_urls or []
    if len(photos) < MIN_PHOTOS:
        # Rejected here rather than left for the worker to bounce off, so the owner
        # gets the number of photos they are short by instead of an empty project.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Need at least {MIN_PHOTOS} photos to generate; "
                f"this project has {len(photos)}"
            ),
        )

    project.status = "processing"
    await db.commit()

    from app.workers.ai_studio_generator import generate_assets

    generate_assets.delay(project.id)

    return AIGenerateResponse(
        project_id=str(project.id),
        status="processing",
        message="Generation started. This usually takes a couple of minutes.",
    )


# ── I9 — project status and assets ───────────────────────────────────────────
@router.get(
    "/ai/projects/{project_id}",
    response_model=AIProjectResponse,
    dependencies=[Owner],
)
async def get_project(
    project_id: str, db: Db, user: CurrentUser
) -> AIProjectResponse:
    """Read a project and its generated assets. Safe to poll while generating."""
    business_id = await resolve_business_id(db, user)
    project = await _project(db, business_id, project_id)

    assets = await _assets_for(db, project.id)
    return _project_out(project, assets, len(project.photo_urls or []))


# ── listing ──────────────────────────────────────────────────────────────────
@router.get("/ai/projects", response_model=list[AIProjectResponse], dependencies=[Owner])
async def list_projects(db: Db, user: CurrentUser) -> list[AIProjectResponse]:
    """All of the caller's projects, newest first.

    Not in the spec, but the studio has no other way to show a shop its history —
    without this, an owner who regenerates can only reach old projects by guessing
    ids.
    """
    business_id = await resolve_business_id(db, user)

    projects = (
        await db.scalars(
            select(AIProject)
            .where(AIProject.business_id == business_id)
            .order_by(AIProject.created_at.desc())
        )
    ).all()

    out: list[AIProjectResponse] = []
    for project in projects:
        assets = await _assets_for(db, project.id)
        out.append(_project_out(project, assets, len(project.photo_urls or [])))
    return out
