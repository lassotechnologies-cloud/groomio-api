"""R2 key-building and type-validation tests (Doc 11.4).

These test the part of storage that is pure logic and therefore testable without
credentials: which filenames are accepted, what the key looks like, and whether
two shops can collide. The presigning itself is boto3's job and is not mocked
here — a test that asserts a mock returned what the mock was configured to
return proves nothing.

The path-traversal cases matter more than they look. These keys are used by the
AI Studio worker to read photos and by the chat client to render attachments, so
a key of `other-business/chat/x.jpg` obtained by traversal would be a cross-tenant
file read.
"""

import pytest

from app.core.storage import (
    ALLOWED_IMAGE_EXT,
    ALLOWED_VIDEO_EXT,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    build_key,
    check_content_type,
    max_bytes_for,
)


# ── key construction ────────────────────────────────────────────────────────
def test_key_is_namespaced_by_tenant():
    key = build_key("biz_123", "image", "logo.jpg")
    assert key.startswith("biz_123/image/")
    assert key.endswith(".jpg")


def test_key_contains_no_user_path_component():
    """`../../admin/keys.png` must yield a key inside the caller's own prefix.

    The filename is reduced to its validated extension; everything else in the
    key is a generated uuid. This is the property that makes traversal
    impossible rather than merely unlikely.
    """
    key = build_key("biz_123", "image", "../../admin/keys.png")
    assert key.startswith("biz_123/image/")
    assert ".." not in key
    assert key.endswith(".png")


def test_two_shops_uploading_the_same_filename_do_not_collide():
    a = build_key("biz_1", "image", "logo.jpg")
    b = build_key("biz_2", "image", "logo.jpg")
    assert a != b


def test_the_same_shop_twice_does_not_overwrite():
    """A stable `shop/logo.jpg` would let a former employee guess the old file."""
    a = build_key("biz_1", "image", "logo.jpg")
    b = build_key("biz_1", "image", "logo.jpg")
    assert a != b


def test_extension_case_is_normalised():
    key = build_key("biz_1", "image", "LOGO.JPG")
    assert key.endswith(".jpg")


# ── allowlist ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ext", sorted(ALLOWED_IMAGE_EXT))
def test_every_allowed_image_extension_is_accepted(ext: str):
    assert build_key("b", "image", f"photo{ext}").endswith(ext)


@pytest.mark.parametrize("name", ["payload.html", "x.svg", "run.exe", "a.php", "x.js"])
def test_non_media_extensions_are_rejected(name: str):
    """SVG and HTML served from the API's own domain are stored XSS."""
    with pytest.raises(ValueError):
        build_key("b", "image", name)


def test_a_file_with_no_extension_is_rejected():
    with pytest.raises(ValueError):
        build_key("b", "image", "justafilename")


def test_a_photo_claimed_as_a_video_is_rejected():
    with pytest.raises(ValueError):
        build_key("b", "video", "clip.jpg")


def test_a_video_claimed_as_an_image_is_rejected():
    """Otherwise a 100MB video can be signed under the smaller image cap."""
    with pytest.raises(ValueError):
        build_key("b", "image", "clip.mp4")


def test_video_extensions_are_accepted_as_videos():
    for ext in sorted(ALLOWED_VIDEO_EXT):
        assert build_key("b", "video", f"clip{ext}").endswith(ext)


# ── content type ↔ extension binding ────────────────────────────────────────
def test_matching_content_type_is_accepted():
    check_content_type("image/jpeg", "b/image/x.jpg")


def test_html_content_type_is_rejected_even_with_an_image_key():
    """The extension alone must not be what decides this."""
    with pytest.raises(ValueError):
        check_content_type("text/html", "b/image/x.jpg")


def test_content_type_must_be_a_real_media_type():
    with pytest.raises(ValueError):
        check_content_type("application/x-msdownload", "b/image/x.jpg")


# ── size limits ─────────────────────────────────────────────────────────────
def test_video_gets_a_larger_cap_than_images():
    assert max_bytes_for("video") == MAX_VIDEO_BYTES
    assert max_bytes_for("image") == MAX_IMAGE_BYTES
    assert MAX_IMAGE_BYTES < MAX_VIDEO_BYTES
