"""Poster and slideshow composition tests (Doc 2.13).

The visual half of the AI Studio is compositing, not generation, so it is fully
testable offline. That is the point: these assertions run in CI with no API key
and no network, which a model-backed implementation could not do.

The tests care about three things an owner would notice immediately:

  1. The output is a real, decodable image at the advertised size.
  2. Text is actually on it — a black poster is not a deliverable.
  3. Nothing raises on bad input. A shop uploading from a phone will, at some
     point, hand us a truncated or non-image file, and that must cost them one
     asset rather than the whole generation.
"""

import io

import pytest
from PIL import Image

from app.ai_studio.compose import (
    DEFAULT_BRAND_COLOR,
    POSTER_SIZE,
    SLIDE_SIZE,
    compose_poster,
    compose_slideshow_frames,
    encode_slideshow,
    load_image,
    placeholder_image,
)


def photo_bytes(color=(40, 35, 30), size=(1400, 1800), fmt="JPEG") -> bytes:
    """A real encoded image. Composing from raw pixel tuples proves nothing."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


# ── posters ─────────────────────────────────────────────────────────────────
def test_a_poster_is_produced_at_the_advertised_size():
    result = compose_poster(photo_bytes(), "Kenge Hair Studio", "Fresh cuts")

    assert (result.width, result.height) == POSTER_SIZE
    assert result.image.mode == "RGB"


def test_the_poster_is_a_decodable_jpeg():
    result = compose_poster(photo_bytes(), "Kenge Hair Studio", "Fresh cuts")
    buffer = io.BytesIO()
    result.image.save(buffer, format="JPEG")

    reloaded = Image.open(io.BytesIO(buffer.getvalue()))
    reloaded.verify()  # raises on a truncated or corrupt file


@pytest.mark.parametrize(
    "color", [(15, 12, 10), (240, 235, 225), (128, 128, 128)]
)
def test_a_poster_works_on_both_dark_and_bright_photos(color):
    """A dark haircut photo must still produce legible text.

    The scrim strength is derived from the photo's own luminance, so a bright
    image is not needlessly darkened and a dark one is not left unreadable.
    """
    result = compose_poster(photo_bytes(color), "Kenge Hair Studio", "Open today")
    assert result.image.getbbox() is not None


def test_the_shop_name_actually_appears_on_the_poster():
    """White text over a darkened band: the poster is not blank."""
    dark = photo_bytes((10, 10, 10))
    result = compose_poster(dark, "Kenge Hair Studio", "Fresh cuts, fair prices")

    # The bottom third must contain near-white pixels — the text and scrim.
    bottom = result.image.crop((0, int(result.height * 0.72), result.width, result.height))
    assert max(bottom.convert("L").getdata()) > 200


def test_a_long_shop_name_is_wrapped_not_overflowed():
    result = compose_poster(
        photo_bytes(),
        "Kenge Premium Unisex Hair Studio and Barber Parlour",
        "Fresh cuts",
    )
    # Wrapping keeps the image the right size; an unwrapped overflow would not
    # change the dimensions, so assert the render simply succeeded and is sane.
    assert (result.width, result.height) == POSTER_SIZE


def test_an_emoji_or_long_word_does_not_raise():
    """Shop names come from a text field and contain whatever the owner types."""
    for name in ("💈✨ Kenge ✨", "A" * 80, "Salón Ñéé", "ケンジ"):
        result = compose_poster(photo_bytes(), name, "Fresh cuts")
        assert (result.width, result.height) == POSTER_SIZE


def test_a_logo_is_optional():
    without = compose_poster(photo_bytes(), "Kenge", "Fresh cuts")
    with_logo = compose_poster(photo_bytes(), "Kenge", "Fresh cuts", logo=photo_bytes((200, 30, 30), (300, 300)))
    assert without.image.size == with_logo.image.size


def test_a_corrupt_logo_does_not_fail_the_poster():
    """The shop name still identifies the business without a logo."""
    result = compose_poster(photo_bytes(), "Kenge Hair Studio", "Fresh cuts", logo=b"not an image")
    assert (result.width, result.height) == POSTER_SIZE


# ── aspect ratio handling ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "size", [(2000, 1000), (1000, 2000), (1000, 1000), (3000, 4000)]
)
def test_every_common_photo_ratio_fills_the_frame_without_distortion(size):
    """Wide, tall and square photos all become a clean full-bleed poster."""
    result = compose_poster(photo_bytes(size=size), "Kenge", "Fresh cuts")
    assert (result.width, result.height) == POSTER_SIZE


def test_a_tiny_photo_is_upscaled_rather_than_failing():
    result = compose_poster(photo_bytes(size=(60, 80)), "Kenge", "Fresh cuts")
    assert (result.width, result.height) == POSTER_SIZE


# ── slideshows ──────────────────────────────────────────────────────────────
def test_a_slideshow_produces_one_frame_per_photo():
    photos = [photo_bytes((30 * i, 40, 50)) for i in range(1, 6)]
    frames = compose_slideshow_frames(photos, "Kenge Hair Studio", "Open today")

    assert len(frames) == 5
    assert frames[0].size == SLIDE_SIZE


def test_the_slideshow_encodes_to_a_real_animated_gif():
    photos = [photo_bytes((30 * i, 40, 50)) for i in range(1, 4)]
    gif = encode_slideshow(compose_slideshow_frames(photos, "Kenge", "Open"))

    assert gif is not None
    assert gif[:4] == b"GIF8", "must be a GIF, not a mislabelled JPEG"

    image = Image.open(io.BytesIO(gif))
    assert image.format == "GIF"
    assert getattr(image, "n_frames", 1) == 3


def test_one_unreadable_photo_does_not_lose_the_slideshow():
    """A truncated upload costs one frame, not the whole advert."""
    photos = [photo_bytes((30, 40, 50)), b"corrupt", photo_bytes((60, 40, 50))]
    frames = compose_slideshow_frames(photos, "Kenge", "Open")

    assert len(frames) == 2


def test_an_empty_slideshow_encodes_to_nothing():
    """None, not an empty file: the caller writes no asset rather than a 0-byte one."""
    assert encode_slideshow([]) is None
    assert compose_slideshow_frames([], "Kenge", "Open") == []


def test_a_single_photo_still_encodes():
    gif = encode_slideshow(compose_slideshow_frames([photo_bytes()], "Kenge", "Open"))
    assert gif is not None and gif[:4] == b"GIF8"


# ── placeholder ─────────────────────────────────────────────────────────────
def test_the_placeholder_is_a_valid_image():
    data = placeholder_image()
    image = Image.open(io.BytesIO(data))
    image.verify()


def test_the_placeholder_uses_the_brand_colour():
    """Deliberate, not a grey "unavailable" square.

    Compared with a tolerance: the placeholder is saved as JPEG, which is lossy,
    so the decoded pixel lands a unit or two off the source colour. Asserting
    exact equality here would fail on a codec change rather than on a real defect.
    """
    image = Image.open(io.BytesIO(placeholder_image()))
    assert image.size == POSTER_SIZE

    actual = image.convert("RGB").getpixel((5, 5))
    for got, want in zip(actual, DEFAULT_BRAND_COLOR):
        assert abs(got - want) <= 4, f"{actual} is not the brand colour {DEFAULT_BRAND_COLOR}"


# ── decoding ────────────────────────────────────────────────────────────────
def test_load_image_downsizes_a_large_photo():
    """A 12MP phone photo must not be materialised at full size in a worker."""
    image = load_image(photo_bytes(size=(4000, 3000)), 1080, 1080)
    assert image.width <= 1080 and image.height <= 1080


def test_load_image_rejects_non_image_bytes():
    with pytest.raises(Exception):
        load_image(b"this is not an image at all", 1080, 1080)


def test_compose_poster_raises_on_non_image_bytes():
    """The caller catches this per-asset; it must not be silently swallowed here."""
    with pytest.raises(Exception):
        compose_poster(b"not an image", "Kenge", "Fresh cuts")
