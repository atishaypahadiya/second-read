#!/usr/bin/env python3
"""Generate extension/icons/icon{16,32,48,128}.png: red rounded tile with a white 'skip forward' glyph.
Pure stdlib (zlib PNG writer), supersampled for smooth edges."""
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "icons")
RED, WHITE = (229, 57, 53), (255, 255, 255)


def png(width, height, rows):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def inside_tile(x, y, s, r):
    """Rounded square covering [0,s] with corner radius r."""
    cx = min(max(x, r), s - r)
    cy = min(max(y, r), s - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def inside_glyph(x, y, s):
    """Two forward chevrons + a bar, all in unit coordinates of the tile."""
    u, v = x / s, y / s
    # chevron(x0): points (x0, .30) (x0+.22, .50) (x0, .70), thickness .11
    def chevron(x0):
        if not (0.30 <= v <= 0.70):
            return False
        edge = x0 + 0.22 - abs(v - 0.50) * 1.1  # outer edge x at this v
        return edge - 0.12 <= u <= edge
    bar = 0.66 <= u <= 0.76 and 0.30 <= v <= 0.70
    return chevron(0.22) or chevron(0.44) or bar


def render(size, ss=4):
    S = size * ss
    r = S * 0.22
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            tile = glyph = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px * ss + sx + 0.5, py * ss + sy + 0.5
                    if inside_tile(x, y, S, r):
                        tile += 1
                        if inside_glyph(x, y, S):
                            glyph += 1
            n = ss * ss
            a = tile / n
            if a == 0:
                row += [0, 0, 0, 0]
                continue
            g = glyph / max(tile, 1)
            col = [round(RED[i] * (1 - g) + WHITE[i] * g) for i in range(3)]
            row += col + [round(255 * a)]
        rows.append(row)
    return png(size, size, rows)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for size in (16, 32, 48, 128):
        with open(os.path.join(OUT, f"icon{size}.png"), "wb") as f:
            f.write(render(size))
        print("wrote", f"icon{size}.png")
