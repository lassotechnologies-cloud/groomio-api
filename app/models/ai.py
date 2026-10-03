"""AI Marketing Studio (Doc 2.13 / 9.9)."""

from sqlalchemy import Column, String, Enum, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, TimestampMixin, uuid_pk


class AIProject(Base, TimestampMixin):
    """Owner uploads shop name, logo and ≥5 photos → system generates assets."""

    __tablename__ = "ai_projects"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    shop_name = Column(String(160), nullable=False)
    logo_url = Column(String, nullable=True)
    photo_urls = Column(JSONB, nullable=False)  # ≥5 photos
    status = Column(
        Enum("uploaded", "processing", "ready", name="ai_project_status"),
        default="uploaded",
        nullable=False,
    )


class AIAsset(Base, TimestampMixin):
    """Generated marketing assets (posters, captions, slideshow video)."""

    __tablename__ = "ai_assets"

    id = uuid_pk()
    project_id = Column(String, nullable=False, index=True)
    asset_type = Column(
        Enum(
            "fb_post",
            "ig_post",
            "whatsapp_status",
            "tiktok_caption",
            "poster",
            "slideshow_video",
            name="ai_asset_type",
        ),
        nullable=False,
    )
    content_text = Column(Text, nullable=True)
    file_url = Column(String, nullable=True)
