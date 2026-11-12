#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""移动网专用直播清单。

为什么单独一份：
    移动 IPTV 的 IPv6 源在电信宽带上 TCP 都连不上（跨运营商封闭），
    所以这类源无法在本地测速、也无法验证。硬塞进主清单会污染电信那份的排序。
    这里只做「按频道归拢 + 去重 + 限量」，不做任何存活判断，原样透传。

用法：
    python live/build_mobile.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import net  # noqa: E402
from build_live import match_score, parse_m3u, parse_txt  # noqa: E402

SOURCES = [
    ("szyyds-大合集", "https://szyyds.cn/tv/live/x.txt"),
    ("饭太硬-ipv6", "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/ipv6.m3u"),
    ("饭太硬-高清合集", "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/itv.m3u"),
]
OUT = os.path.join(ROOT, "dist")
KEEP_PER_CHANNEL = 8


def load_channels(path: str) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            name = parts[0]
            aliases = [a.strip() for a in (parts[1] if len(parts) > 1 else "").split(",") if a.strip()]
            group = parts[2] if len(parts) > 2 and parts[2] else "其他"
            out.append({"name": name, "aliases": aliases, "group": group})
    return out


def is_v6_mobile(url: str) -> bool:
    """2409::/16 是中国移动的 IPv6 段。

    注意 urlsplit().hostname 会把方括号去掉，所以这里不能匹配 "[2409:"。
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    return host.startswith("2409:")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    channels = load_channels(os.path.join(HERE, "channels.txt"))
    pool = []
    seen = set()
    for label, url in SOURCES:
        try:
            text, code, ms, used = net.fetch_text_best(url, timeout=30)
        except Exception as e:  # noqa: BLE001
            print("[采集] %-16s 失败 %s" % (label, net._short(e)))
            continue
        items = parse_m3u(text) if "#EXTM3U" in text[:2048].upper() else parse_txt(text)
        n0 = len(pool)
        for it in items:
            u = it.get("url") or ""
            if not is_v6_mobile(u) or u in seen:
                continue
            seen.add(u)
            it["src"] = label
            pool.append(it)
        print("[采集] %-16s %5d 条，其中移动 IPv6 新增 %d" % (label, len(items), len(pool) - n0))

    picked = {}
    for ch in channels:
        scored = []
        for it in pool:
            sc = match_score(ch, it.get("tvg_name") or it.get("name") or "")
            if sc >= 55:
                scored.append((sc, it))
        scored.sort(key=lambda x: (-x[0], x[1].get("src", "")))
        seen_u = set()
        lines = []
        for sc, it in scored:
            if it["url"] in seen_u:
                continue
            seen_u.add(it["url"])
            lines.append(it)
            if len(lines) >= KEEP_PER_CHANNEL:
                break
        picked[ch["name"]] = (ch, lines)

    out_path = os.path.join(OUT, "live-mobile.m3u")
    os.makedirs(OUT, exist_ok=True)
    total = 0
    cover = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("#EXTM3U\n")
        for name, (ch, lines) in picked.items():
            if lines:
                cover += 1
            for it in lines:
                nm = "%s（移动）" % ch["name"]
                fh.write('#EXTINF:-1 tvg-name="%s" group-title="%s",%s\n'
                         % (ch["name"], ch["group"], nm))
                fh.write(it["url"] + "\n")
                total += 1
    print("[输出] %s" % out_path)
    print("[输出] 覆盖 %d/%d 个频道，共 %d 条线路（未做任何可用性验证）" % (cover, len(channels), total))
    miss = [n for n, (c, l) in picked.items() if not l]
    if miss:
        print("[缺口] " + "、".join(miss))
    with open(os.path.join(OUT, "live-mobile-report.json"), "w", encoding="utf-8") as fh:
        json.dump({"channels": {n: len(l) for n, (c, l) in picked.items()},
                   "total": total, "cover": cover}, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
