"""Messaging & notification schemas (Doc 10.10 — I1–I6)."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ThreadResponse(BaseModel):
    """I1 — a booking-scoped conversation.

    `last_message_preview` is already masked, because it comes from stored
    message bodies, which are masked at write time. The client never sees a raw
    number to leak somewhere else.
    """

    id: str
    branch_id: str
    customer_id: str
    customer_name: Optional[str] = None
    appointment_id: Optional[str] = None
    queue_entry_id: Optional[str] = None
    status: str
    last_message_at: Optional[datetime] = None
    last_message_preview: Optional[str] = None
    unread_count: int = 0


class MessageCreateRequest(BaseModel):
    """I3 — text and/or a photo. `body` is required only when there is no
    attachment, so an empty message cannot be sent by accident."""

    body: str = Field("", max_length=4000)
    attachment_url: Optional[str] = Field(
        None,
        max_length=500,
        description="R2 object key. Access is via a short-lived signed URL.",
    )

    def validate_not_empty(self) -> "MessageCreateRequest":
        if not self.body.strip() and not self.attachment_url:
            raise ValueError("A message needs text or a photo")
        return self


class MessageResponse(BaseModel):
    """I2 — one message, with read state.

    `body` is the masked text as stored. There is no field for the original: it
    was never persisted, so it cannot be leaked by a different endpoint.
    """

    id: str
    thread_id: str
    sender_side: str
    sender_name: Optional[str] = None
    body: str
    attachment_url: Optional[str] = None
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # Tells the sender their number was removed, so the masking is visible
    # rather than mysterious. Only set on the message that triggered it.
    phone_masked: bool = False


class MessageSendResponse(MessageResponse):
    """I3 — what the sender gets back, including the filtering outcome."""

    filtered: bool = False
    rejection_reason: Optional[str] = None


class PushSubscribeRequest(BaseModel):
    """I4 — a browser push subscription (the JSON the Push API hands out)."""

    endpoint: str = Field(..., min_length=1, max_length=1000)
    keys: dict = Field(..., description="p256dh and auth keys from the Push API")


class PushSubscribeResponse(BaseModel):
    registered: bool = True
    endpoint: str


class NotificationResponse(BaseModel):
    """I5 — one inbox row."""

    id: str
    type: str
    title: str
    body: str
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class CampaignCreateRequest(BaseModel):
    """I6 — an in-app or push blast. `scheduled_at` in the past sends now."""

    title: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=4000)
    channel: Literal["in_app", "push"]
    segment: Literal["all", "inactive_30d", "birthday_month", "loyalty_tier"]
    scheduled_at: Optional[datetime] = None


class CampaignResponse(BaseModel):
    id: str
    business_id: str
    channel: str
    segment: str
    title: str
    body: str
    scheduled_at: Optional[datetime] = None
    status: str
    recipient_count: int = 0
    created_at: Optional[datetime] = None
