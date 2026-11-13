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
KEEP_PER_CHANNEL = 6
FALLBACK_PER_CHANNEL = 3
GATEWAY_HOST = "183.237.95.108"


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


def is_tokenized(url: str) -> bool:
    """魔百和的 /wh7f454c46tw.../ 跳转路径带有时效 token，放后面当备选。"""
    return "/wh7f454c46" in url


def load_vetted(path: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """把主清单里已经实测可用的公网线路读进来，作为移动版的兜底。

    移动那条线的价值是「比公网更稳」，但它没法在本机验证；万一带 token 的
    地址全失效，兜底线路至少保证电视上有得看。运营商网关那条单独放，
    只在某台完全没有别的线路时才拿出来用。
    """
    pub: dict[str, list[str]] = {}
    gw: dict[str, list[str]] = {}
    if not os.path.exists(path):
        return pub, gw
    cur = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("#EXTINF"):
                cur = line.rsplit(",", 1)[-1].split(" · ")[0].strip()
            elif line and not line.startswith("#") and cur:
                if GATEWAY_HOST and GATEWAY_HOST in line:
                    gw.setdefault(cur, []).append(line)
                    continue
                lst = pub.setdefault(cur, [])
                if len(lst) < FALLBACK_PER_CHANNEL:
                    lst.append(line)
    return pub, gw


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
        # 带 wh7f454c46... 的是魔百和的加密跳转路径，里面的 token 会过期；
        # 不带 token 的直连地址更耐用，所以同分情况下优先排前面。
        scored.sort(key=lambda x: (-x[0], is_tokenized(x[1]["url"]), x[1].get("src", "")))
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

    vetted, gw_only = load_vetted(os.path.join(OUT, "live.m3u"))
    out_path = os.path.join(OUT, "live-mobile.m3u")
    os.makedirs(OUT, exist_ok=True)
    total = 0
    cover = 0
    n_vet = 0
    per_ch: dict[str, int] = {}
    picked_urls = {u for _, (c, ls) in picked.items() for u in [it["url"] for it in ls]}
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("#EXTM3U\n")
        for name, (ch, lines) in picked.items():
            for it in lines:
                nm = "%s（移动）" % ch["name"]
                fh.write('#EXTINF:-1 tvg-name="%s" group-title="%s",%s\n'
                         % (ch["name"], ch["group"], nm))
                fh.write(it["url"] + "\n")
                total += 1
                per_ch[name] = per_ch.get(name, 0) + 1
            for u in vetted.get(name, []):
                if u in picked_urls:
                    continue
                fh.write('#EXTINF:-1 tvg-name="%s" group-title="%s",%s（公网兜底）\n'
                         % (ch["name"], ch["group"], ch["name"]))
                fh.write(u + "\n")
                total += 1
                n_vet += 1
                per_ch[name] = per_ch.get(name, 0) + 1
            if not lines and not vetted.get(name):
                # 移动源和公网源都没有，只剩运营商网关那一条，总比空着强
                for u in gw_only.get(name, [])[:1]:
                    fh.write('#EXTINF:-1 tvg-name="%s" group-title="%s",%s（网关兜底）\n'
                             % (ch["name"], ch["group"], ch["name"]))
                    fh.write(u + "\n")
                    total += 1
                    n_vet += 1
                    per_ch[name] = per_ch.get(name, 0) + 1
    cover = sum(1 for v in per_ch.values() if v)
    print("[输出] %s" % out_path)
    print("[输出] 覆盖 %d/%d 个频道，共 %d 条线路（移动 %d 未验证 / 公网兜底 %d 已验证）"
          % (cover, len(channels), total, total - n_vet, n_vet))
    miss = [n for n in (c["name"] for c in channels) if not per_ch.get(n)]
    if miss:
        print("[缺口] " + "、".join(miss))
    with open(os.path.join(OUT, "live-mobile-report.json"), "w", encoding="utf-8") as fh:
        json.dump({"channels": per_ch, "total": total, "cover": cover,
                   "vetted": n_vet, "unverified": total - n_vet},
                  fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
