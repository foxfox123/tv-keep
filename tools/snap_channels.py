#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给运营商 IPTV 网关的每个编号抽一帧画面，拼成带编号的网格图，
方便人工/视觉识别「编号 -> 台名」。

用法：
    python tools/snap_channels.py 183.237.95.108:9901 1000-1106
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMES = os.path.join(ROOT, "dist", "frames-hd")
GRIDS = os.path.join(ROOT, "dist", "grids")

COLS, ROWS = 6, 5
CELL_W, CELL_H, LABEL_H = 320, 180, 26


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


FF = ffmpeg_exe()


def snap(host: str, num: int) -> tuple:
    out = os.path.join(FRAMES, "%d.jpg" % num)
    if os.path.exists(out) and os.path.getsize(out) > 3000:
        return num, True
    url = "http://%s/tsfile/live/%d_1.m3u8?key=txiptv&playlive=0&authid=0" % (host, num)
    for pre in ("2", "6", "0"):
        cmd = [FF, "-hide_banner", "-loglevel", "quiet", "-y"]
        if pre != "0":
            cmd += ["-ss", pre]
        # 保留 1280 宽，台标才有足够像素；压到 320 宽就认不出台名了
        cmd += ["-i", url, "-frames:v", "1", "-an",
                "-vf", "scale=1280:-2", out]
        try:
            subprocess.run(cmd, timeout=40, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except Exception:  # noqa: BLE001
            continue
        if os.path.exists(out) and os.path.getsize(out) > 3000:
            return num, True
    return num, False


def build_grids(nums):
    from PIL import Image, ImageDraw, ImageFont
    os.makedirs(GRIDS, exist_ok=True)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    per = COLS * ROWS
    paths = []
    for gi in range(0, len(nums), per):
        chunk = nums[gi:gi + per]
        canvas = Image.new("RGB", (COLS * CELL_W, ROWS * (CELL_H + LABEL_H)), (24, 24, 24))
        draw = ImageDraw.Draw(canvas)
        for i, num in enumerate(chunk):
            r, c = divmod(i, COLS)
            x, y = c * CELL_W, r * (CELL_H + LABEL_H)
            p = os.path.join(FRAMES, "%d.jpg" % num)
            if os.path.exists(p):
                try:
                    im = Image.open(p).convert("RGB")
                    im = im.resize((CELL_W, CELL_H))
                    canvas.paste(im, (x, y))
                except Exception:  # noqa: BLE001
                    draw.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(60, 20, 20))
            else:
                draw.rectangle([x, y, x + CELL_W, y + CELL_H], fill=(50, 50, 50))
                draw.text((x + 100, y + 70), "no signal", fill=(200, 200, 200), font=font)
            draw.rectangle([x, y + CELL_H, x + CELL_W, y + CELL_H + LABEL_H], fill=(0, 0, 0))
            draw.text((x + 8, y + CELL_H + 3), "No.%d" % num, fill=(0, 255, 120), font=font)
            draw.rectangle([x, y, x + CELL_W - 1, y + CELL_H + LABEL_H - 1],
                           outline=(90, 90, 90))
        out = os.path.join(GRIDS, "grid-%02d.jpg" % (gi // per + 1))
        canvas.save(out, quality=82)
        paths.append(out)
    return paths


def parse_range(s: str):
    m = re.match(r"^(\d+)-(\d+)$", s)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    host, spec = sys.argv[1], sys.argv[2]
    os.makedirs(FRAMES, exist_ok=True)
    nums = parse_range(spec)
    print("抽帧 %s 的 %d 个编号，ffmpeg=%s" % (host, len(nums), os.path.basename(FF)))
    ok = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for i, (num, good) in enumerate(ex.map(lambda n: snap(host, n), nums), 1):
            if good:
                ok.append(num)
            if i % 20 == 0:
                print("  %d/%d 成功 %d" % (i, len(nums), len(ok)))
    print("抽帧成功 %d/%d" % (len(ok), len(nums)))
    grids = build_grids(ok)
    for g in grids:
        print("  网格 ->", g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
