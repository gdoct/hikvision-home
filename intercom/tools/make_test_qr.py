"""
Print sheet for testing the QR scanner (app/qr.py).

One A4 page with the same code at four sizes, so one walk to the gate answers
"how big does a code have to be, and how close do I have to hold it". Each
tile is labelled with its printed width; the decoded text carries the size
too, so /api/qr/log says which tile was read without you remembering.

Run it (the image already has zxing-cpp and pillow):

    docker run --rm -v "$HOME/Downloads:/out" intercom-app:latest \
        python tools/make_test_qr.py /out/gate-qr-test.pdf

Print at 100% / "actual size", NOT "fit to page", or the labels lie.
"""

from __future__ import annotations

import sys

import zxingcpp
from PIL import Image, ImageDraw, ImageFont

DPI = 300
A4 = (int(8.27 * DPI), int(11.69 * DPI))
MM = DPI / 25.4
# Printed width of each code, in millimetres.
SIZES_MM = (100, 60, 40, 25)
TEXT = "GATE-TEST-{mm}MM"


def qr_image(text: str, px: int) -> Image.Image:
    """A QR code as a PIL image of px by px, no anti-aliasing (NEAREST keeps
    the modules square, which is what a scanner wants)."""
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    raw = memoryview(barcode.to_image(size_hint=200))
    h, w = raw.shape[:2]
    img = Image.frombuffer("L", (w, h), raw.tobytes())
    return img.resize((px, px), Image.NEAREST)


def font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def main(out: str) -> None:
    page = Image.new("L", A4, 255)
    draw = ImageDraw.Draw(page)
    title = font(int(5 * MM))
    label = font(int(4 * MM))

    draw.text((int(15 * MM), int(12 * MM)), "Intercom QR scanner - test sheet", font=title, fill=0)
    draw.text(
        (int(15 * MM), int(19 * MM)),
        "Print at 100% (actual size). Hold each code up to the intercom for a few seconds,",
        font=label, fill=0,
    )
    draw.text(
        (int(15 * MM), int(25 * MM)),
        "starting with the biggest. Then check /api/qr/log on the intercom app.",
        font=label, fill=0,
    )

    y = int(35 * MM)
    for mm in SIZES_MM:
        px = int(mm * MM)
        text = TEXT.format(mm=mm)
        page.paste(qr_image(text, px), (int(15 * MM), y))
        draw.text((int(15 * MM) + px + int(8 * MM), y + int(4 * MM)), f"{mm} mm", font=title, fill=0)
        draw.text((int(15 * MM) + px + int(8 * MM), y + int(13 * MM)), text, font=label, fill=0)
        y += px + int(8 * MM)

    page.convert("RGB").save(out, resolution=DPI)
    print(f"wrote {out} ({', '.join(f'{mm}mm' for mm in SIZES_MM)})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "gate-qr-test.pdf")
