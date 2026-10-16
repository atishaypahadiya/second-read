#!/usr/bin/env python3
"""Keep each mockup PNG under LIMIT bytes: saved as is when it fits, else palette-quantized (fewer colors until it
fits). Prints one line per file. Needs Pillow."""
import os
import sys

from PIL import Image

LIMIT = 300 * 1024

for path in sys.argv[1:]:
    img = Image.open(path).convert("RGB")
    img.save(path, optimize=True)
    how = "as is"
    for colors in (256, 192, 128, 96, 64):
        if os.path.getsize(path) <= LIMIT:
            break
        img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(path, optimize=True)
        how = f"{colors} colors"
    print(f"{os.path.relpath(path)}: {img.width}x{img.height}, {os.path.getsize(path) / 1024:.0f} KB ({how})"
          + ("" if os.path.getsize(path) <= LIMIT else "  OVER LIMIT"))
