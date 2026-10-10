#!/usr/bin/env python3
"""Render plugins/yobiru/assets/icon.png from the same geometry as assets/logo.svg (stdlib only).

Two tailnet nodes joined by a link, with a wake ring around the receiving node. 4x4 supersampling,
RGBA output; run it again only when the logo changes.
"""
import math
import struct
import zlib
from pathlib import Path

SIZE, SS = 256, 4
BLUE, WHITE = (0x4C, 0x6E, 0xF5), (0xFF, 0xFF, 0xFF)
A, B = (88.0, 168.0), (168.0, 88.0)


def in_round_rect(x, y, r=56.0):
    cx = min(max(x, r), SIZE - r)
    cy = min(max(y, r), SIZE - r)
    return 0 <= x <= SIZE and 0 <= y <= SIZE and (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / (vx * vx + vy * vy)))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))


def blend(dst, src, alpha):
    return tuple(d * (1 - alpha) + s * alpha for d, s in zip(dst, src))


def sample(x, y):
    """-> (r, g, b, a) for one sample point, painting in SVG order."""
    if not in_round_rect(x, y):
        return (0.0, 0.0, 0.0, 0.0)
    rgb, p = BLUE, (x, y)
    if 42.0 <= dist(p, B) <= 52.0:
        rgb = blend(rgb, WHITE, 0.55)
    if seg_dist(p, A, B) <= 10.0:
        rgb = WHITE
    if dist(p, A) <= 28.0 or dist(p, B) <= 28.0:
        rgb = WHITE
    if dist(p, A) <= 10.0 or dist(p, B) <= 10.0:
        rgb = BLUE
    return rgb + (255.0,)


def render():
    rows = []
    for py in range(SIZE):
        row = bytearray([0])  # PNG filter type 0
        for px in range(SIZE):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    r, g, b, a = sample(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS)
                    w = a / 255.0
                    acc[0] += r * w
                    acc[1] += g * w
                    acc[2] += b * w
                    acc[3] += a
            n = SS * SS
            alpha = acc[3] / n
            if alpha:
                cov = alpha / 255.0
                row += bytes(int(round(c / n / cov)) for c in acc[:3]) + bytes([int(round(alpha))])
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return b"".join(rows)


def png(raw):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "plugins" / "yobiru" / "assets" / "icon.png"
    out.write_bytes(png(render()))
    print("wrote", out)
