#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性工具：从公开 m3u 集合里挖运营商 IPTV 单播源（tsfile 类），
在家宽下实测哪些真能拉、真实码率多少。

不参与日常管道，只在需要补充高质量源时手动跑一次。

用法：
    python tools/harvest_iptv.py               # 挖 + 测
    python tools/harvest_iptv.py --no-probe    # 只挖不测
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "harvest.json")
POOL = os.path.join(ROOT, "dist", "harvest-pool.txt")

# imDazui 仓库里与「单播」有关的文件（组播 rtp/udp 一律跳过，家宽用不了）
REPO = "https://cdn.jsdelivr.net/gh/imDazui/Tvlist-awesome-m3u-m3u8@master/m3u/"
FILES = [
    "上海电信.m3u", "上海电信iptv.m3u", "上海电信iptv.m3u8",
    "云南昆明电信.m3u", "内蒙古电信单播rtsp.m3u",
    "四川成都电信IPTV.m3u", "四川电信.m3u", "四川电信IPTV.m3u",
    "安徽电信.m3u", "安徽电信IPTV.m3u", "安徽联通IPTV.m3u",
    "山东电信2025.m3u", "山东电信IPTV带台标.m3u", "山东电信2021-11.m3u",
    "广东iptv.m3u", "广东电信IPTV.m3u", "广西联通IPTV.m3u",
    "江苏南京电信.m3u", "江苏南京电信2.m3u", "江苏泰州电信.m3u",
    "江苏电信.m3u", "江苏电信.m3u8", "江西电信.m3u",
    "河北电信IPTV.m3u", "浙江电信.m3u", "湖北电信.m3u",
    "湖南电信IPTV.m3u", "甘肃电信组播.m3u",
    "北京移动IPTV.m3u", "云南移动IPTV.m3u", "山东移动IPTV201912.m3u",
    "江苏移动iptv.m3u", "浙江移动IPTV.m3u",
    "CCTV.m3u", "_CCTVHD.m3u8", "china.m3u",
]

BROWSER = {"User-Agent": "okhttp/3.15"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BAD_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|ico|css|js|xml|html?)(\?|$)", re.I)


def fetch(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_m3u(text: str):
    """返回 [(name, url)]，兼容 #EXTINF 与 txt 两种写法。"""
    out = []
    name = None
    for line in text.splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s:
            continue
        if s.startswith("#EXTINF"):
            m = re.search(r",([^,]*?)(?:\$.*)?$", s)
            name = m.group(1).strip() if m else ""
            if not name:
                m2 = re.search(r'tvg-name="([^"]+)"', s)
                name = m2.group(1) if m2 else ""
        elif s.startswith("#"):
            continue
        else:
            if s.lower().startswith(("http://", "https://")):
                out.append((name or "?", s.split("$")[0].strip()))
            name = None
    return out


def collect():
    seen = {}
    for f in FILES:
        u = REPO + urllib.parse.quote(f)
        try:
            txt = fetch(u)
        except Exception as e:  # noqa: BLE001
            print("  ! %-26s %s" % (f, type(e).__name__))
            continue
        items = parse_m3u(txt)
        new = 0
        for n, url in items:
            if BAD_EXT.search(url):
                continue
            key = url.lower()
            if key in seen:
                continue
            seen[key] = {"name": n, "url": url, "file": f}
            new += 1
        print("  · %-26s %4d 条（新增 %d）" % (f, len(items), new))
    return list(seen.values())


def tcp_ok(url: str, timeout: float = 5) -> bool:
    p = urllib.parse.urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except Exception:  # noqa: BLE001
        return False
    for fam, typ, proto, _, addr in infos[:2]:
        s = socket.socket(fam, typ, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            return True
        except Exception:  # noqa: BLE001
            continue
        finally:
            s.close()
    return False


def http_playlist(url: str, timeout: float = 8):
    """拉播放列表，返回 (ok, 首分片地址, 分片时长, 分辨率, 带宽)。"""
    try:
        req = urllib.request.Request(url, headers=BROWSER)
        with OPENER.open(req, timeout=timeout) as r:
            body = r.read(200 * 1024).decode("utf-8", "replace")
            real = r.geturl()
    except Exception:  # noqa: BLE001
        return False, None, None, None, None
    if not body.lstrip().startswith("#EXTM3U"):
        return False, None, None, None, None
    res = re.search(r"RESOLUTION=(\d+)x(\d+)", body)
    bw = re.search(r"BANDWIDTH=(\d+)", body)
    # 多码率：钻进第一个子列表
    uris = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    if res and uris:
        try:
            sub = urllib.parse.urljoin(real, uris[0])
            return http_playlist(sub, timeout)
        except Exception:  # noqa: BLE001
            pass
    dur = re.search(r"#EXTINF:([\d.]+)", body)
    if not uris or not dur:
        return False, None, None, None, None
    return True, urllib.parse.urljoin(real, uris[0]), float(dur.group(1)), \
        (res.group(1) + "x" + res.group(2)) if res else None, \
        int(bw.group(1)) if bw else None


def seg_kbps(seg: str, dur: float, timeout: float = 10) -> int:
    """取分片字节数 / 分片时长 = 码率。与网速无关，是源的固有属性。"""
    if not dur:
        return 0
    try:
        req = urllib.request.Request(seg, headers=BROWSER, method="HEAD")
        with OPENER.open(req, timeout=timeout) as r:
            size = int(r.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        size = 0
    if not size:
        try:
            req = urllib.request.Request(seg, headers=BROWSER)
            with OPENER.open(req, timeout=timeout) as r:
                size = len(r.read(3 * 1024 * 1024))
        except Exception:  # noqa: BLE001
            return 0
    return round(size * 8 / dur / 1000)


def probe(item: dict) -> dict:
    url = item["url"]
    out = dict(item, ok=False, kbps=0, ttfb=None, res=None, note="")
    if not tcp_ok(url):
        out["note"] = "TCP 不通"
        return out
    t0 = time.time()
    ok, seg, dur, res, bw = http_playlist(url)
    out["ttfb"] = round((time.time() - t0) * 1000)
    if not ok:
        out["note"] = "不是有效 m3u8"
        return out
    out["res"] = res
    out["kbps"] = seg_kbps(seg, dur)
    out["ok"] = out["kbps"] > 0
    out["note"] = "%.1fs 分片" % dur if dur else "无分片时长"
    return out


def main():
    no_probe = "--no-probe" in sys.argv
    print("[1/3] 拉取源文件")
    items = collect()
    print("[1/3] 合计 %d 条去重线路" % len(items))
    if not items:
        return 1

    with open(POOL, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write("%s\t%s\t%s\n" % (it["name"], it["url"], it["file"]))
    print("[1/3] 原始清单 -> %s" % POOL)
    if no_probe:
        return 0

    print("[2/3] 实测 %d 条（TCP + 拉列表 + 量分片）" % len(items))
    results = []
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        futs = [ex.submit(probe, it) for it in items]
        done = 0
        for fut in cf.as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 100 == 0:
                print("  ... %d/%d" % (done, len(items)))
    alive = [r for r in results if r["ok"]]
    alive.sort(key=lambda r: -r["kbps"])
    print("[3/3] 可用 %d 条（TCP 通 %d 条）"
          % (len(alive), sum(1 for r in results if r["note"] != "TCP 不通")))
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "total": len(items), "alive": len(alive),
                   "items": alive}, fh, ensure_ascii=False, indent=1)
    print("[3/3] 结果 -> %s" % OUT)
    for r in alive[:20]:
        print("  %6d kbps  %-22s %-14s %s"
              % (r["kbps"], r["name"][:22], r["res"] or "-", r["url"][:70]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
