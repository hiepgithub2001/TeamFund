#!/usr/bin/env python3
"""Draw the pay page's PNG icons (a football on green) with only the standard library.

    python3 scripts/make-web-icons.py      -> server/app/interfaces/web/icon-192.png, icon-512.png

Same picture as icon.svg: 512-unit design, 4x4 supersampling for smooth edges. The ball and its pentagon sit
inside the central 80%, so the icon also works as a maskable (cropped) icon."""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "server/app/interfaces/web"
GREEN, WHITE = (0x1F, 0x7A, 0x4D), (0xFF, 0xFF, 0xFF)
PENT = [(256, 186), (322, 234), (297, 312), (215, 312), (190, 234)]
SPOKES = [((256, 186), (256, 112)), ((322, 234), (392, 211)), ((297, 312), (340, 372)),
          ((215, 312), (172, 372)), ((190, 234), (120, 211))]


def inside_poly(x, y, pts):
    c = False
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i], pts[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def near_segment(x, y, a, b, r):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    u = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (ax + u * dx), y - (ay + u * dy)) <= r


def color(x, y):
    """The design at one point, in 512x512 units. None = transparent (outside the rounded square)."""
    r = 112
    cx, cy = min(max(x, r), 512 - r), min(max(y, r), 512 - r)
    if math.hypot(x - cx, y - cy) > r:
        return None
    if math.hypot(x - 256, y - 256) <= 150:
        if inside_poly(x, y, PENT) or any(near_segment(x, y, a, b, 7) for a, b in SPOKES):
            return GREEN
        return WHITE
    return GREEN


def png(size: int) -> bytes:
    ss, rows = 4, []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc, n = [0, 0, 0, 0], 0
            for sy in range(ss):
                for sx in range(ss):
                    c = color((px + (sx + .5) / ss) * 512 / size, (py + (sy + .5) / ss) * 512 / size)
                    n += 1
                    if c:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            a = acc[3] // n
            row += bytes([acc[0] // max(1, acc[3] // 255), acc[1] // max(1, acc[3] // 255),
                          acc[2] // max(1, acc[3] // 255), a] if a else [0, 0, 0, 0])
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for size in (192, 512):
        (OUT / f"icon-{size}.png").write_bytes(png(size))
        print(f"icon-{size}.png")
