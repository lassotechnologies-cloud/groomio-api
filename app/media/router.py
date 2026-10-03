"""Signed upload URLs — Doc 11.4 (media).

The upload contract is two requests, not a multipart POST:

  1. `POST /media/upload-url` with a filename and content type. The API validates
     the type, builds a tenant-scoped key, and returns a short-lived PUT URL.
  2. The client PUTs the bytes straight to R2 and sends the returned `key` (not
     the URL) in whatever record is being created.

Step 2 goes to R2 rather than through us so a 40MB slideshow video uploaded on
mobile data crosses the network once. Nothing in this router receives a file
body, which is also why there is no upload-size limit to enforce here — the size
cap is communicated to the client and enforced by the signature's expiry.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser
from app.core.storage import (
    ALLOWED_IMAGE_EXT,
    ALLOWED_VIDEO_EXT,
    max_bytes_for,
    presign_upload,
    storage_configured,
)
from app.core.tenancy import resolve_business_id
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["media"])

Db = Annotated[AsyncSession, Depends(get_db)]


class UploadUrlRequest(BaseModel):
    """Ask for permission to upload one file."""

    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(..., min_length=3, max_length=100)
    kind: Literal["image", "video"] = "image"


class UploadUrlResponse(BaseModel):
    """Where to PUT, what to store, and how long it is good for.

    `key` is the value to persist. `upload_url` expires and must never be stored
    or logged — it is a bearer credential for the object.
    """

    key: str
    upload_url: str
    expires_in: int
    max_bytes: int
    content_type: str


@router.post("/media/upload-url", response_model=UploadUrlResponse)
async def create_upload_url(
    payload: UploadUrlRequest, db: Db, user: CurrentUser
) -> UploadUrlResponse:
    """Presign a direct-to-R2 upload for the caller's business."""
    if not storage_configured():
        # 503, not 500: the credentials are a deployment problem, not a bug, and
        # telling the client to retry later is the honest answer.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File uploads are not configured on this environment",
        )

    business_id = await resolve_business_id(db, user)

    try:
        presigned = presign_upload(
            business_id,
            payload.kind,
            payload.filename,
            payload.content_type,
        )
    except ValueError as exc:
        # The extension/content-type allowlist rejecting a file is a client
        # error, so 422 with the reason rather than a 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return UploadUrlResponse(
        key=presigned.key,
        upload_url=presigned.upload_url,
        expires_in=presigned.expires_in,
        max_bytes=max_bytes_for(payload.kind),
        content_type=payload.content_type,
    )


@router.get("/media/limits")
async def media_limits() -> dict:
    """What the client should offer in its file picker.

    Cheaper than having every client hardcode these and drift from the server,
    which is how a barber ends up unable to upload a video that the API would
    have accepted.
    """
    return {
        "image": {
            "extensions": sorted(ALLOWED_IMAGE_EXT),
            "max_bytes": max_bytes_for("image"),
        },
        "video": {
            "extensions": sorted(ALLOWED_VIDEO_EXT),
            "max_bytes": max_bytes_for("video"),
        },
        "storage_configured": storage_configured(),
    }
