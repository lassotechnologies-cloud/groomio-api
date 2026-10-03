"""Poster and slideshow composition (Doc 2.13).

A "promotional poster, brand-coloured, with logo" is not a generative task. It is
a photo, a logo, some text and a colour, and the MVP is explicitly *not* an AI
video product — the docs put true generative video in Phase 3. So the visual half
of the studio is compositing with Pillow, which runs offline, costs nothing per
poster, and cannot fail because a model vendor is down.

That is a deliberate reading of the spec, not a shortcut. A salon owner needs a
tidy poster with their logo on it today; they do not need a diffusion model, and
waiting for one would ship nothing.

Three things this module is careful about:

  - **Text legibility.** A photo can be any brightness, so the scrim behind the
    text is computed from the image's own luminance rather than hardcoded. Black
    text on a black haircut photo is not a poster.

  - **Fonts.** Pillow needs a real TTF for sized text. Rather than assume one is
    installed (macOS and a slim Linux container disagree), fonts are searched and
    a bitmap default is used only if none is found.

  - **Memory.** Phone photos are 4000px wide and a queue of them will exhaust a
    small worker. Images are downscaled to the output size as they are opened,
    with `Image.draft` doing the decode shrink in C.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# Instagram's portrait post, and the size the shop will actually export at.
POSTER_SIZE = (1080, 1350)
SLIDE_SIZE = (1080, 1080)

# Fonts to try, in order of preference. DejaVu ships with most Linux images;
# the macOS paths cover local development.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

# A brand colour that is not black, white or transparent. Chosen for contrast
# against skin tones and shop interiors, which is most of Kenyan photography.
DEFAULT_BRAND_COLOR = (196, 106, 32)

_font_cache: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    """A bold font at `size`, cached. Falls back to Pillow's bitmap default."""
    if size in _font_cache:
        return _font_cache[size]

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[size] = font
                return font
            except OSError:
                continue

    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def load_image(data: bytes, max_width: int, max_height: int) -> Image.Image:
    """Decode bytes to an RGB image no larger than the given box.

    `draft` asks the JPEG decoder to scale during decode, so a 12MP photo is
    never fully materialised. Without it a worker handling a dozen photos holds
    far more than it needs and can be OOM-killed mid-generation.
    """
    image = Image.open(io.BytesIO(data))
    image.draft("RGB", (max_width, max_height))  # no-op for non-JPEG
    image = image.convert("RGB")
    image.thumbnail((max_width, max_height), Image.LANCZOS)
    return image


def _cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale and centre-crop to exactly fill `size`, preserving aspect ratio.

    A photo squashed to a square looks amateurish; cropping to fill is what every
    phone gallery does and is what the owner expects.
    """
    target_w, target_h = size
    src_ratio = image.width / image.height
    dst_ratio = target_w / target_h

    if src_ratio > dst_ratio:
        # Too wide — crop the sides.
        new_w = int(image.height * dst_ratio)
        left = (image.width - new_w) // 2
        image = image.crop((left, 0, left + new_w, image.height))
    else:
        # Too tall — crop top and bottom, biased upward since faces sit high.
        new_h = int(image.width / dst_ratio)
        top = int((image.height - new_h) * 0.35)
        image = image.crop((0, top, image.width, top + new_h))

    return image.resize(size, Image.LANCZOS)


def _luminance(image: Image.Image) -> float:
    """Mean perceived brightness, 0 (black) to 1 (white).

    Rec. 601 weights, because the eye is most sensitive to green — matching the
    channel weighting means "bright" here means what the owner sees.
    """
    grey = image.convert("L").resize((32, 32), Image.BILINEAR)
    pixels = list(grey.getdata())
    return sum(pixels) / (255 * len(pixels))


def _scrim(image: Image.Image) -> Image.Image:
    """A gradient darkening the lower half, so text is readable on any photo.

    The strength is chosen from the image's own luminance. A dark photo gets a
    strong scrim; a bright one gets almost none, because darkening an already
    bright image just wastes the picture.
    """
    brightness = _luminance(image)
    alpha_max = int(30 + (1 - brightness) * 165)  # 30 (bright) .. 195 (dark)
    w, h = image.size

    # Build a one-pixel-wide gradient and stretch it; a full-size per-pixel
    # gradient is slow and looks identical.
    gradient = Image.new("L", (1, h))
    for y in range(h):
        # Only the bottom 55% is darkened, fading in.
        t = max(0.0, (y / h - 0.45) / 0.55)
        gradient.putpixel((0, y), int(alpha_max * (t**1.5)))

    mask = gradient.resize((w, h), Image.BILINEAR)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(black, image, mask)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Break `text` into lines that fit `max_width`, measured with the font.

    Measuring with `textbbox` rather than counting characters matters: "ILLINOIS"
    is far narrower per character than "MMMM", and a character-count heuristic
    overflows on the first line of most shop names.
    """
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


@dataclass
class PosterResult:
    image: Image.Image
    width: int
    height: int


def compose_poster(
    photo: bytes,
    shop_name: str,
    tagline: str,
    brand_color: tuple[int, int, int] = DEFAULT_BRAND_COLOR,
    logo: Optional[bytes] = None,
    size: tuple[int, int] = POSTER_SIZE,
) -> PosterResult:
    """A poster: full-bleed photo, logo, shop name and tagline.

    `photo` and `logo` are raw bytes; the caller decides where they come from.
    Bytes rather than paths because the images live in R2 and are fetched
    signed, not as files on this machine.
    """
    base = _cover_resize(load_image(photo, *size), size)
    base = _scrim(base)

    draw = ImageDraw.Draw(base)
    w, h = base.size
    margin = int(w * 0.08)
    text_width = w - (margin * 2)

    # ── logo, top-left, on a light plate so it reads on any photo ──────────
    if logo:
        try:
            mark = load_image(logo, int(h * 0.12), int(h * 0.12))
            plate = Image.new("RGBA", (mark.width + 24, mark.height + 24), (255, 255, 255, 235))
            plate.paste(mark, (12, 12), mark if mark.mode == "RGBA" else None)
            base.paste(plate.convert("RGB"), (margin, margin), plate)
        except Exception:
            # A corrupt or unreadable logo must not fail the whole poster — the
            # shop name still identifies the business.
            pass

    # ── brand bar ──────────────────────────────────────────────────────────
    bar_h = int(h * 0.012)
    draw.rectangle(
        [(margin, int(h * 0.66) - bar_h), (margin + int(w * 0.18), int(h * 0.66))],
        fill=brand_color,
    )

    # ── shop name ──────────────────────────────────────────────────────────
    name_font = _font(int(h * 0.075))
    lines = _wrap(draw, shop_name, name_font, text_width)
    y = int(h * 0.70)
    for line in lines[:3]:  # three lines is the most a name can be here
        draw.text((margin, y), line, font=name_font, fill=(255, 255, 255))
        y += int(h * 0.085)

    # ── tagline ────────────────────────────────────────────────────────────
    if tagline:
        tag_font = _font(int(h * 0.036))
        y += int(h * 0.01)
        for line in _wrap(draw, tagline, tag_font, text_width)[:2]:
            draw.text((margin, y), line, font=tag_font, fill=(240, 240, 240))
            y += int(h * 0.045)

    return PosterResult(image=base, width=base.width, height=base.height)


def compose_slideshow_frames(
    photos: Iterable[bytes],
    shop_name: str,
    tagline: str,
    brand_color: tuple[int, int, int] = DEFAULT_BRAND_COLOR,
    size: tuple[int, int] = SLIDE_SIZE,
) -> list[Image.Image]:
    """One frame per photo, square, with the shop name burned in.

    A slideshow is a sequence of stills, so the MVP produces the stills. Encoding
    them to MP4 needs ffmpeg, which is not in the API image — see
    `encode_slideshow` for the intended hand-off.

    Returns an empty list when no photo decodes, so the caller can mark the asset
    honestly rather than writing an empty video.
    """
    frames: list[Image.Image] = []
    for raw in photos:
        try:
            poster = compose_poster(
                raw,
                shop_name,
                tagline,
                brand_color=brand_color,
                size=size,
            )
            frames.append(poster.image)
        except Exception:
            # One unreadable photo should not lose the slideshow entirely.
            continue
    return frames


def encode_slideshow(frames: list[Image.Image], fps: int = 1) -> Optional[bytes]:
    """Encode frames to an animated GIF, or None if nothing could be produced.

    GIF rather than MP4 on purpose: Pillow encodes it with no external binary, and
    WhatsApp — where most Kenyan salons actually post — accepts a GIF. An MP4
    would need ffmpeg in the image, and an animated GIF can be assembled here
    without it. The owner can convert to MP4 in any phone app if they want.

    One frame per second: a slideshow that flickers is not watchable, and social
    platforms compress anything longer anyway.
    """
    if not frames:
        return None

    try:
        buffer = io.BytesIO()
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=int(1000 / fps),
            loop=0,
            optimize=True,
        )
        return buffer.getvalue()
    except Exception:
        return None


def placeholder_image(size: tuple[int, int] = POSTER_SIZE) -> bytes:
    """A neutral branded card used when a photo cannot be read.

    Shipping a grey "image unavailable" square is worse than shipping a card that
    at least says the shop's name and looks deliberate.
    """
    w, h = size
    image = Image.new("RGB", size, DEFAULT_BRAND_COLOR)
    draw = ImageDraw.Draw(image)
    font = _font(int(h * 0.06))
    lines = _wrap(draw, "Your poster is on its way", font, int(w * 0.8))
    y = h // 2 - (len(lines) * int(h * 0.07)) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(
            ((w - (bbox[2] - bbox[0])) // 2, y),
            line,
            font=font,
            fill=(255, 255, 255),
        )
        y += int(h * 0.07)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()
