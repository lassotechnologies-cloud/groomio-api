"""Request/response JSON shapes — Pydantic v2 schemas (Doc 10 conventions)."""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Standard error envelope: {"error": {"code": "...", "message": "..."}}."""

    error: dict = Field(
        ..., example={"code": "queue_full", "message": "The queue is full"}
    )


class MessageResponse(BaseModel):
    message: str
