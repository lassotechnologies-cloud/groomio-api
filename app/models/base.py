"""Shared declarative base + timestamp mixin (Doc 9 naming rule).

Every table has `id` (UUID PK), `created_at`, `updated_at` unless noted.
Business-scoped models carry `business_id`; branch-scoped models carry
`branch_id` — the tenancy filter enforces isolation at query time.
"""

import uuid
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Column, DateTime, func
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class TimestampMixin:
    """created_at / updated_at on every table."""

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def uuid_pk():
    return Column(
        UUID,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
