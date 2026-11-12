# -*- coding: utf-8 -*-
"""共用网络工具：只用 Python 标准库，零第三方依赖。

设计原则：
1. 任何单个地址失败都不能让整个任务崩掉，一律返回结构化结果。
2. 探测要说真话——只认实际读到的字节，不猜。
"""
from __future__ import annotations

import gzip
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = (
    "Mozilla/5.0 (Linux; Android 12; SmartTV) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

EXTM3U = re.compile(rb"^\s*#EXTM3U", re.I)


def _short(e: BaseException) -> str:
    """把异常压缩成一行人话。"""
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %s" % e.code
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, socket.timeout):
            return "超时"
        return str(r)[:60]
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "超时"
    return "%s: %s" % (type(e).__name__, str(e)[:60])


_GH_RAW = re.compile(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")
_GH_JSD = re.compile(r"^https?://cdn\.jsdelivr\.net/gh/([^/]+)/([^/@]+)@([^/]+)/(.+)$")


def alternate(url: str) -> str | None:
    """GitHub raw 与 jsDelivr 互为镜像。

    raw.githubusercontent.com 在国内经常解析不了，而在境外 CI 上又偶尔很慢；
    jsDelivr 反过来。两边能互转，就等于给每个订阅地址都配了个备胎。
    """
    m = _GH_RAW.match(url)
    if m:
        owner, repo, branch, path = m.groups()
        return "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (owner, repo, branch, path)
    m = _GH_JSD.match(url)
    if m:
        owner, repo, branch, path = m.groups()
        return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, branch, path)
    return None


def fetch_text_best(
    url: str, timeout: float = 20
) -> tuple[str, int, float, str]:
    """先直连，失败自动换镜像重试一次。返回 (内容, 状态码, 耗时, 实际用的地址)。"""
    try:
        text, code, ms = fetch_text(url, timeout=timeout)
        return text, code, ms, url
    except Exception:  # noqa: BLE001
        alt = alternate(url)
        if not alt:
            raise
        text, code, ms = fetch_text(alt, timeout=timeout)
        return text, code, ms, alt


def classify_error(e: BaseException) -> tuple[str, str]:
    """判断一个异常意味着“真死”还是“可疑”。

    这是本项目最重要的一条判断：跑到这台机器多半在境外（GitHub Actions），
    而国内源对境外 IP 常常返回 403 或干脆不响应。这类失败**不能当成站点已死**，
    否则每天自动跑一次，几天就把好源全删光了。只有全球都够不着的才算死。
    """
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (404, 410, 451):
            return "dead", "HTTP %s" % e.code
        if e.code in (401, 403, 407):
            return "suspect", "HTTP %s（可能地域限制）" % e.code
        return "suspect", "HTTP %s" % e.code
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, socket.gaierror):
            return "dead", "域名解析失败"
        if isinstance(r, ConnectionRefusedError):
            return "dead", "连接被拒绝"
        if isinstance(r, socket.timeout):
            return "suspect", "超时"
        txt = str(r)[:50]
        if "Name or service not known" in txt or "nodename nor servname" in txt:
            return "dead", "域名解析失败"
        return "suspect", txt
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "suspect", "超时"
    if isinstance(e, ssl.SSLError):
        return "suspect", "TLS 握手失败"
    return "suspect", "%s: %s" % (type(e).__name__, str(e)[:50])


def encode_url(url: str) -> str:
    """处理中文域名和含中文的路径，urllib 默认会直接抛 ascii 编码错误。"""
    if all(ord(c) < 128 for c in url):
        return url
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc
    if any(ord(c) >= 128 for c in netloc):
        userinfo, host = "", netloc
        if "@" in host:
            userinfo, host = host.rsplit("@", 1)
            userinfo += "@"
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:
            host = urllib.parse.quote(host, safe=":")
        netloc = userinfo + host
    return urllib.parse.urlunsplit((
        parts.scheme,
        netloc,
        urllib.parse.quote(parts.path, safe="/%"),
        urllib.parse.quote(parts.query, safe="=&%?/:+"),
        urllib.parse.quote(parts.fragment, safe="/%"),
    ))


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        h.update({k: v for k, v in headers.items() if v})
    return urllib.request.Request(encode_url(url), headers=h)


def fetch_bytes(
    url: str,
    timeout: float = 15,
    headers: dict | None = None,
    limit: int = 8 * 1024 * 1024,
) -> tuple[bytes, int, float]:
    """抓取文本/配置类内容。返回 (内容, 状态码, 耗时毫秒)。"""
    t0 = time.time()
    with urllib.request.urlopen(_request(url, headers), timeout=timeout, context=_CTX) as r:
        buf = bytearray()
        while len(buf) < limit:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
        enc = (r.headers.get("Content-Encoding") or "").lower()
        raw = bytes(buf)
        if "gzip" in enc and raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                pass
        return raw, r.status, (time.time() - t0) * 1000


def fetch_text(
    url: str, timeout: float = 15, headers: dict | None = None
) -> tuple[str, int, float]:
    raw, code, ms = fetch_bytes(url, timeout=timeout, headers=headers)
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc), code, ms
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), code, ms


def _probe_leech(
    url: str, timeout: float, sample: int, max_seconds: float, headers: dict | None
) -> dict:
    """真正读一段字节，测首字节时间与速率。"""
    out = {"ok": False, "kbps": 0, "ttfb_ms": None, "note": ""}
    t0 = time.time()
    try:
        with urllib.request.urlopen(_request(url, headers), timeout=timeout, context=_CTX) as r:
            first = r.read(4096)
            out["ttfb_ms"] = round((time.time() - t0) * 1000)
            total = len(first)
            if total < 188 and not first:
                out["note"] = "空响应"
                return out
            while total < sample:
                if time.time() - t0 > max_seconds:
                    break
                chunk = r.read(32768)
                if not chunk:
                    break
                total += len(chunk)
            dt = max(time.time() - t0, 0.001)
            if total < 8192 and time.time() - t0 > max_seconds:
                out["note"] = "速率过低"
            if total < 2048:
                out["note"] = out["note"] or "数据过少"
                return out
            out["ok"] = True
            if dt > 0.3:
                out["kbps"] = round(total * 8 / dt / 1000)
            return out
    except Exception as e:  # noqa: BLE001
        out["note"] = _short(e)
        return out


def _first_uri(playlist: bytes) -> str | None:
    for line in playlist.splitlines():
        s = line.strip()
        if not s or s.startswith(b"#"):
            continue
        try:
            return s.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def probe_stream(
    url: str,
    timeout: float = 8,
    sample: int = 200_000,
    max_seconds: float = 5.0,
    headers: dict | None = None,
    deep: bool = True,
) -> dict:
    """探测一条直播流。

    返回 {ok, cls, kind, kbps, ttfb_ms, note}
      cls: alive（确认可播）/ suspect（可能是地域或网络原因）/ dead（全球都够不着）
      kind: playlist(HLS) / ts(裸流) / dead

    deep=False 只验证播放列表本身，不去拉分片。在境外机器上跑时用它：
    分片能不能拉，很大程度取决于 CDN 怎么给你回源，海外测出来的结果对国内没意义。
    """
    info = {"ok": False, "cls": "dead", "kind": "dead", "kbps": 0, "ttfb_ms": None, "note": ""}
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        info["note"] = "非 http(s)，客户端拉不到"
        return info

    t0 = time.time()
    try:
        with urllib.request.urlopen(_request(url, headers), timeout=timeout, context=_CTX) as r:
            head = r.read(4096)
            info["ttfb_ms"] = round((time.time() - t0) * 1000)
            if head.lstrip()[:7].upper() == b"#EXTM3U":
                body = head + r.read(96 * 1024)
                info["kind"] = "playlist"
                # 无信号/广告占位：短循环列表
                segs = body.count(b"#EXTINF")
                if b"#EXT-X-ENDLIST" in body and segs <= 3:
                    info["cls"], info["note"] = "suspect", "疑似占位/广告循环"
                    return info
                uri = _first_uri(body)
                if not uri:
                    info["cls"], info["note"] = "suspect", "播放列表里没有分片"
                    return info
                if not deep:
                    info.update(ok=True, cls="alive", note="播放列表正常")
                    return info
                seg = urllib.parse.urljoin(r.geturl(), uri)
                leech = _probe_leech(seg, timeout, sample, max_seconds, headers)
                info["ok"] = leech["ok"]
                info["cls"] = "alive" if leech["ok"] else "suspect"
                info["kbps"] = leech["kbps"]
                info["note"] = leech["note"] or "HLS 分片正常"
                if leech["ttfb_ms"] and not info["ttfb_ms"]:
                    info["ttfb_ms"] = leech["ttfb_ms"]
                return info

            # 非播放列表，当作裸流直接读
            info["kind"] = "ts"
            total = len(head)
            limit = sample if deep else 8192
            while total < limit and time.time() - t0 <= max_seconds:
                chunk = r.read(32768)
                if not chunk:
                    break
                total += len(chunk)
            dt = max(time.time() - t0, 0.001)
            if total < 2048:
                info["cls"], info["note"] = "suspect", "数据过少"
                return info
            info["ok"] = True
            info["cls"] = "alive"
            info["kbps"] = round(total * 8 / dt / 1000) if dt > 0.3 else 0
            info["note"] = "裸流正常"
            return info
    except Exception as e:  # noqa: BLE001
        info["cls"], info["note"] = classify_error(e)
        return info


def normalize(name: str) -> str:
    """频道名归一化：去空格标点、去画质后缀，便于匹配。"""
    s = name.lower()
    s = re.sub(r"[（(\[【].*?[)）\]】]", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("－", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"[·•\._,、|/\\:：\-_]", "", s)
    s = re.sub(r"(高清|超清|标清|蓝光|原画|流畅|画质|测试|备用|hd|fhd|uhd|4k|8k|1080p?|720p?|2160p?)", "", s)
    return s.strip()
