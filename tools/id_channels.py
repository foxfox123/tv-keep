#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 TS 流里读频道名（DVB SDT 表）。

运营商 IPTV 的 tsfile 网关只给编号不给台名，但流里自带 SDT，
其中 service_descriptor 就写着台名。这比任何外部映射表都准。

用法：
    python tools/id_channels.py 183.237.95.108:9901 1000-1106
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "channels-id.json")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "okhttp/3.15"}


def fetch(url: str, limit: int = 400_000, timeout: float = 10) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with OPENER.open(req, timeout=timeout) as r:
        return r.read(limit)


def first_seg(host: str, num: int) -> str | None:
    u = "http://%s/tsfile/live/%d_1.m3u8?key=txiptv&playlive=0&authid=0" % (host, num)
    try:
        body = fetch(u, 4096, 8).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    if not body.lstrip().startswith("#EXTM3U"):
        return None
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return urllib.parse.urljoin(u, s)
    return None


def parse_sdt(data: bytes) -> str | None:
    """扫 TS 包，找 PID 0x11 的 SDT，解出第一个 service_name。"""
    buf = b""
    i = 0
    n = len(data)
    while i + 188 <= n:
        if data[i] != 0x47:
            i += 1
            continue
        pkt = data[i:i + 188]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != 0x11:
            i += 188
            continue
        pusi = pkt[1] & 0x40
        afc = (pkt[3] >> 4) & 0x03
        off = 4
        if afc in (2, 3):
            off = 4 + 1 + pkt[4]
        if afc == 2 or off >= 188:
            i += 188
            continue
        payload = pkt[off:]
        if pusi:
            if not payload:
                i += 188
                continue
            ptr = payload[0]
            buf = payload[1 + ptr:]
        else:
            buf += payload
        name = _parse_sdt_section(buf)
        if name:
            return name
        i += 188
    return None


def _parse_sdt_section(b: bytes) -> str | None:
    if len(b) < 12 or b[0] != 0x42:
        return None
    sec_len = ((b[1] & 0x0F) << 8) | b[2]
    end = min(len(b), 3 + sec_len - 4)  # 去掉 CRC32
    p = 11  # 跳过 TSID/version/section/orig_network_id
    while p + 5 <= end:
        p += 2  # service_id
        p += 1  # reserved/flags
        dlen = ((b[p] & 0x0F) << 8) | b[p + 1]
        p += 2
        dstart = p
        dend = min(p + dlen, end)
        q = dstart
        while q + 2 <= dend:
            tag = b[q]
            ln = b[q + 1]
            body = b[q + 2:q + 2 + ln]
            if tag == 0x48 and len(body) >= 3:
                # service_type(1) provider_len(1) provider name_len(1) name
                pl = body[1]
                off = 2 + pl
                if off < len(body):
                    nl = body[off]
                    raw = body[off + 1:off + 1 + nl]
                    for enc in ("utf-8", "gbk", "gb18030"):
                        try:
                            s = raw.decode(enc).strip("\x00 ").strip()
                            if s and sum(1 for ch in s if ch.isprintable()) >= len(s) * 0.7:
                                return s
                        except Exception:  # noqa: BLE001
                            continue
            q += 2 + ln
        p = dend
    return None


def probe(host: str, num: int) -> dict:
    out = {"num": num, "ok": False, "name": None, "note": ""}
    seg = first_seg(host, num)
    if not seg:
        out["note"] = "无列表"
        return out
    try:
        data = fetch(seg, 400_000, 10)
    except Exception as e:  # noqa: BLE001
        out["note"] = type(e).__name__
        return out
    out["ok"] = True
    if not data or data[0] != 0x47:
        out["note"] = "非 TS"
        return out
    out["name"] = parse_sdt(data)
    if not out["name"]:
        out["note"] = "无 SDT"
    return out


def parse_range(s: str):
    m = re.match(r"^(\d+)-(\d+)$", s)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    host = sys.argv[1]
    nums = parse_range(sys.argv[2])
    print("探测 %s 的 %d 个编号" % (host, len(nums)))
    res = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(lambda n: probe(host, n), nums):
            res.append(r)
    got = [r for r in res if r["name"]]
    print("拿到台名 %d / 可用 %d" % (len(got), sum(1 for r in res if r["ok"])))
    for r in got:
        print("  %4d  %s" % (r["num"], r["name"]))
    missing = [r["num"] for r in res if r["ok"] and not r["name"]]
    if missing:
        print("无 SDT 的编号：", missing)
    payload = {"host": host, "generated": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
               "map": {str(r["num"]): r["name"] for r in got},
               "alive": [r["num"] for r in res if r["ok"]]}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("-> %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
