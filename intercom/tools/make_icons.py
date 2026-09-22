#!/usr/bin/env python3
"""Generate the home-screen icons (pure stdlib, no Pillow).

A blueprint-style gate in the Industry palette: two pillars, a barred gate
leaf, a hairline frame with "+" registration marks. All content sits inside
the central 80% so the same image is safe as an Android maskable icon.

    python3 tools/make_icons.py        # writes app/static/icons/*.png
"""
import struct, zlib
from pathlib import Path

BG, GRID, FRAME = (0x0d, 0x12, 0x16), (0x15, 0x1b, 0x20), (0x2c, 0x45, 0x5d)
ACCENT, ACCENT_LIGHT, PAPER = (0x59, 0x80, 0xa6), (0x94, 0xbc, 0xe3), (0xf2, 0xf2, 0xf3)


def draw(size: int) -> bytes:
    s = size / 512
    px = [[BG] * size for _ in range(size)]

    def rect(x0, y0, x1, y1, color):
        for y in range(max(0, round(y0 * s)), min(size, max(round(y0 * s) + 1, round(y1 * s)))):
            row = px[y]
            for x in range(max(0, round(x0 * s)), min(size, max(round(x0 * s) + 1, round(x1 * s)))):
                row[x] = color

    def outline(x0, y0, x1, y1, t, color):
        rect(x0, y0, x1, y0 + t, color); rect(x0, y1 - t, x1, y1, color)
        rect(x0, y0, x0 + t, y1, color); rect(x1 - t, y0, x1, y1, color)

    for g in range(0, 512, 64):                      # faint modular grid
        rect(g, 0, g + 2, 512, GRID); rect(0, g, 512, g + 2, GRID)
    outline(112, 112, 400, 400, 3, FRAME)            # hairline frame
    for cx in (112, 400):                            # "+" registration marks
        for cy in (112, 400):
            rect(cx - 16, cy - 3, cx + 17, cy + 3, ACCENT_LIGHT)
            rect(cx - 3, cy - 16, cx + 3, cy + 17, ACCENT_LIGHT)
    rect(132, 328, 380, 334, ACCENT)                 # ground line
    rect(144, 184, 170, 328, ACCENT_LIGHT)           # pillars
    rect(342, 184, 368, 328, ACCENT_LIGHT)
    for bx in (206, 232, 258, 284, 310):             # gate bars
        rect(bx - 3, 212, bx + 3, 304, ACCENT)
    outline(180, 208, 332, 308, 6, PAPER)            # gate leaf

    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180), ("favicon-32.png", 32)):
        (out / name).write_bytes(draw(size))
        print(f"{name}: {size}x{size}")
