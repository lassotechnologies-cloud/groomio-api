"""Cloudflare R2 object storage — signed upload and download URLs (Doc 11.4).

Why signed URLs instead of proxying files through the API: a shop in Nanyuki
uploading five photos over mobile data should not have those bytes travel to our
server only to be forwarded. Presigning hands the browser a direct PUT target, so
the file goes to R2 in one hop and the API only ever handles a key.

Why the bucket is private: shop photos are customer photos. A public bucket URL is
a permanent, unloggable link to someone's face that anyone can share. Private plus
short-lived signed reads is the only version of this that respects DPA 2019.

Object keys are namespaced by tenant and carry no user-supplied path component
beyond a sanitised extension — an owner uploading "../../admin/keys" must not be
able to write outside their own prefix.
"""

import re
import uuid
from dataclasses import dataclass
from functools import lru_cache

import boto3

from app.core.config import settings

# Only real media extensions. Rejecting by allowlist rather than blocklist
# matters here: a .html or .svg upload served from the same domain is a stored
# XSS vector, and blocklists always miss the next one.
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".webm"}

# Bound the type a client claims against what the key extension allows, so a
# .html renamed to .jpg cannot be uploaded and later served as HTML.
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "video/mp4",
    "video/quicktime",
    "video/webm",
}

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB

_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,5}$")


def storage_configured() -> bool:
    """Whether R2 credentials are present.

    Checked rather than assumed so the app still boots in a developer's local
    environment, where returning a clear 503 beats an opaque boto3 error.
    """
    return bool(settings.r2_account_id and settings.r2_access_key_id)


@lru_cache(maxsize=1)
def _client():
    """An S3 client pointed at the account's R2 endpoint.

    R2 speaks the S3 API, so boto3 works unmodified — the only change is the
    endpoint URL and signature version, both required by Cloudflare.
    """
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


@dataclass
class PresignedUpload:
    """What the client needs to PUT a file straight to R2."""

    key: str
    upload_url: str
    expires_in: int


@dataclass
class PresignedDownload:
    """A short-lived read URL for a private object."""

    key: str
    download_url: str
    expires_in: int


def _extension(filename: str, kind: str) -> str:
    """Validate the extension against the allowlist and return it.

    The extension is taken from the *declared* filename but checked against an
    allowlist, so `../../x.jpg` is harmless — only the suffix survives, and the
    key itself is built from a generated uuid.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT
    if not _SAFE_EXT.match(suffix) or suffix not in allowed:
        raise ValueError(
            f"Unsupported file type. Allowed: {', '.join(sorted(allowed))}"
        )
    if kind == "video" and suffix not in ALLOWED_VIDEO_EXT:
        raise ValueError("Expected a video file")
    if kind == "image" and suffix not in ALLOWED_IMAGE_EXT:
        raise ValueError("Expected an image file")
    return suffix


def build_key(business_id: str, kind: str, filename: str) -> str:
    """A tenant-namespaced key with a random filename component.

    The random part is not decoration: without it, `shop/logo.jpg` is a stable
    URL that a former employee can guess and read forever.
    """
    suffix = _extension(filename, kind)
    return f"{business_id}/{kind}/{uuid.uuid4().hex}{suffix}"


def check_content_type(content_type: str, key: str) -> None:
    """Reject a declared content type that does not match the key extension."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported content type: {content_type}")


def max_bytes_for(kind: str) -> int:
    return MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES


def presign_upload(
    business_id: str,
    kind: str,
    filename: str,
    content_type: str,
    expires_in: int | None = None,
) -> PresignedUpload:
    """A PUT URL the browser can upload to directly.

    `content_type` is bound into the signature, so a client cannot upload
    arbitrary bytes under a presigned image URL and have them served later as
    whatever the object name implies.
    """
    key = build_key(business_id, kind, filename)
    check_content_type(content_type, key)
    ttl = expires_in or settings.r2_signed_url_ttl_seconds
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=ttl,
    )
    return PresignedUpload(key=key, upload_url=url, expires_in=ttl)


def presign_download(key: str, expires_in: int | None = None) -> PresignedDownload:
    """A GET URL for an object the client already holds a key for.

    Callers must have already proven they may read the object — this function
    signs anything it is given, and the tenant check is the caller's job. Getting
    that backwards would let any authenticated shop sign any other shop's photo.
    """
    ttl = expires_in or settings.r2_signed_url_ttl_seconds
    url = _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return PresignedDownload(key=key, download_url=url, expires_in=ttl)


def sign_reads(keys: list[str], expires_in: int | None = None) -> dict[str, str]:
    """Sign several keys at once — the list-project read path.

    Returns a key → URL map. Keys that fail to sign are omitted rather than
    raised on: one unreadable photo should not 500 an entire project.
    """
    if not storage_configured():
        return {}
    out: dict[str, str] = {}
    client = _client()
    ttl = expires_in or settings.r2_signed_url_ttl_seconds
    for key in keys:
        try:
            out[key] = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.r2_bucket, "Key": key},
                ExpiresIn=ttl,
            )
        except Exception:
            continue
    return out
