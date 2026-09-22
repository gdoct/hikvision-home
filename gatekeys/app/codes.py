"""
The codes themselves: how they are generated, and how they get onto paper.

Format: GK-XXXX-XXXX-XXXX, from a 32-character alphabet without I, L, O and U
(so nothing is misread aloud or by OCR, and nothing spells a word). That is
60 bits of randomness — unguessable, and short enough to stay a small QR:
uppercase letters, digits and '-' keep it in QR alphanumeric mode, which makes
a version-2 symbol with fat modules instead of a dense one.

Size matters more than it sounds. Measured at the gate on 2026-09-22, a 25 mm
printed code covered 61x54 px in the camera and still decoded; cards here are
40 mm to leave room for movement, angle and worse light.
"""

from __future__ import annotations

import io
import secrets

import zxingcpp
from PIL import Image, ImageDraw, ImageFont

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford-style, no I L O U
DPI = 300
MM = DPI / 25.4
CARD_QR_MM = 40


def new_token() -> str:
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
    return "GK-" + "-".join(groups)


def qr_image(text: str, px: int) -> Image.Image:
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    raw = memoryview(barcode.to_image(size_hint=200))
    height, width = raw.shape[:2]
    img = Image.frombuffer("L", (width, height), raw.tobytes())
    # NEAREST: the modules must stay square-edged, not be blurred into grey.
    return img.resize((px, px), Image.NEAREST)


def qr_png(text: str, px: int = 600) -> bytes:
    buf = io.BytesIO()
    qr_image(text, px).save(buf, "PNG")
    return buf.getvalue()


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
    except OSError:
        return ImageFont.load_default(size)


def card_pdf(token: str, name: str, schedule_lines: list[str], valid: str) -> bytes:
    """An A6 card to hand to someone: the code, who it is for, and when it
    works — so a code found in a drawer in two years explains itself."""
    width, height = int(4.13 * DPI), int(5.83 * DPI)  # A6 portrait
    page = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(page)

    draw.text((int(10 * MM), int(10 * MM)), "GATE KEY", font=_font(int(6 * MM), bold=True), fill=0)
    draw.text((int(10 * MM), int(18 * MM)), name[:40], font=_font(int(5 * MM)), fill=0)

    qr_px = int(CARD_QR_MM * MM)
    page.paste(qr_image(token, qr_px), (int(10 * MM), int(28 * MM)))
    draw.text((int(10 * MM), int(72 * MM)), token, font=_font(int(4.5 * MM), bold=True), fill=0)

    y = int(82 * MM)
    draw.text((int(10 * MM), y), valid, font=_font(int(3.6 * MM)), fill=0)
    y += int(6 * MM)
    for line in schedule_lines[:8]:
        draw.text((int(10 * MM), y), line, font=_font(int(3.6 * MM)), fill=0)
        y += int(5 * MM)

    draw.text(
        (int(10 * MM), height - int(14 * MM)),
        "Hold up to the intercom camera, about an arm's length away.",
        font=_font(int(3.2 * MM)), fill=0,
    )

    buf = io.BytesIO()
    page.convert("RGB").save(buf, "PDF", resolution=DPI)
    return buf.getvalue()
