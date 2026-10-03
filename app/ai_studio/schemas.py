"""AI Studio schemas (Doc 10.10 — I7–I9)."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Doc 2.13: at least five photos. Fewer and the "AI" is just the logo on a
# template, which the owner would have to redo by hand anyway.
MIN_PHOTOS = 5


class AIProjectCreateRequest(BaseModel):
    """I7 — the owner uploads a logo and their shop photos.

    Photo URLs are R2 object keys, not public links: the bucket is private and
    access is via short-lived signed URLs (Doc 11.4).
    """

    shop_name: str = Field(..., min_length=1, max_length=160)
    logo_url: Optional[str] = Field(None, max_length=500)
    photo_urls: list[str] = Field(..., min_length=1, max_length=30)


class AIAssetResponse(BaseModel):
    """One generated asset.

    `file_url` is null while the visual backend is pending. A row that exists
    with no file is honest about progress; a broken image link is not.
    """

    id: str
    asset_type: str
    content_text: Optional[str] = None
    file_url: Optional[str] = None
    created_at: Optional[datetime] = None


class AIProjectResponse(BaseModel):
    """I9 — project status and everything generated so far."""

    id: str
    business_id: str
    shop_name: str
    logo_url: Optional[str] = None
    photo_count: int
    status: Literal["uploaded", "processing", "ready"]
    photos_needed: int = 0
    assets: list[AIAssetResponse] = []
    created_at: Optional[datetime] = None


class AIGenerateResponse(BaseModel):
    """I8 — 202 Accepted. Generation is slow, so the client polls I9."""

    project_id: str
    status: str
    message: str
