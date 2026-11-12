#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已抽好的帧重新拼成更大的网格（帧已在 dist/frames，不重新抽流）。

用法：
    python tools/rebuild_grid.py 4 3 480      # 4列3行，每格480宽
    python tools/rebuild_grid.py 4 3 480 top  # 只拼左上角台标区域（放大2倍）
"""
from __future__ import annotations

import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMES = os.path.join(ROOT, "dist", "frames-hd")
OUT = os.path.join(ROOT, "dist", "grids")


def main():
    cols = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    cw = int(sys.argv[3]) if len(sys.argv) > 3 else 480
    mode = sys.argv[4] if len(sys.argv) > 4 else "full"
    ch = cw * 9 // 16
    label = 28

    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()

    nums = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(os.path.join(FRAMES, "*.jpg"))
                  if os.path.basename(p)[:-4].isdigit())
    per = cols * rows
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "big-*.jpg")):
        os.remove(f)
    out_paths = []
    for gi in range(0, len(nums), per):
        chunk = nums[gi:gi + per]
        canvas = Image.new("RGB", (cols * cw, rows * (ch + label)), (18, 18, 18))
        d = ImageDraw.Draw(canvas)
        for i, num in enumerate(chunk):
            r, c = divmod(i, cols)
            x, y = c * cw, r * (ch + label)
            p = os.path.join(FRAMES, "%d.jpg" % num)
            try:
                im = Image.open(p).convert("RGB")
            except Exception:  # noqa: BLE001
                d.rectangle([x, y, x + cw, y + ch], fill=(50, 50, 50))
                d.text((x + 8, y + ch + 3), "No.%d" % num, fill=(0, 255, 120), font=font)
                continue
            if mode == "top":
                # 只拼左上角台标区域，按原像素贴，不缩放
                im = im.crop((0, 0, cw, ch))
            elif mode == "corners":
                im = im.crop((0, 0, cw, ch))
            else:
                im = im.resize((cw, ch))
            canvas.paste(im, (x, y))
            d.rectangle([x, y + ch, x + cw, y + ch + label], fill=(0, 0, 0))
            d.text((x + 8, y + ch + 4), "No.%d" % num, fill=(0, 255, 120), font=font)
            d.rectangle([x, y, x + cw - 1, y + ch + label - 1], outline=(80, 80, 80))
        out = os.path.join(OUT, "big-%02d.jpg" % (gi // per + 1))
        canvas.save(out, quality=85)
        out_paths.append(out)
    print("共 %d 帧 -> %d 张图" % (len(nums), len(out_paths)))
    for p in out_paths:
        print("  ", p)


if __name__ == "__main__":
    main()
