#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 dist/ 里的产物推到 Cloudflare Worker（KV）。

纯标准库，零依赖。设计要点：
  · 按文件哈希跳过没变化的文件 —— KV 免费版每天只有 1000 次写入，
    而 dist 里有 47 个 repo json，不能每天全量重写。
  · 并发 4 个，太多了反而容易撞上 KV 的写入限速。
  · 推送失败不阻塞本地产物：dist/ 已经写好了，重推一次就行。

用法：
    python cf/push.py                 # 推送
    python cf/push.py --dry           # 只看会推哪些，不发请求
    python cf/push.py --force         # 忽略哈希，全量重推
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(HERE, ".last-push.json")

# 这些只是本机排查用的中间文件，没必要占 KV 的写入次数
SKIP_NAMES = {".DS_Store", ".gitkeep"}
SKIP_SUFFIX = ("-hist.json",)


def load_conf() -> dict:
    conf = {"worker_url": "", "dist": os.path.join(ROOT, "dist")}
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as f:
            conf.update(json.load(f) or {})
    # 配置里留空（模板默认就是这样）时必须回落到默认目录，
    # 否则 conf["dist"] 会变成空字符串，脚本直接找不到产物
    if not conf.get("dist"):
        conf["dist"] = os.path.join(ROOT, "dist")
    # 地址也允许走环境变量，方便换环境
    conf["worker_url"] = (os.environ.get("TVKEEP_WORKER") or conf.get("worker_url") or "").rstrip("/")
    return conf


def load_token() -> str:
    """token 优先从环境变量取，其次 cf/.token 文件（这个文件不进 git）。"""
    tok = os.environ.get("TVKEEP_TOKEN")
    if tok:
        return tok.strip()
    p = os.path.join(HERE, ".token")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    return ""


def walk(dist: str) -> list[tuple[str, str]]:
    """返回 [(KV key, 本地绝对路径)]，key 用正斜杠，保持 repo/ 层级。"""
    out = []
    for base, _dirs, files in os.walk(dist):
        for name in sorted(files):
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIX):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, dist).replace(os.sep, "/")
            out.append((rel, full))
    return out


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def put(base: str, token: str, key: str, data: bytes, timeout: float = 45) -> dict:
    req = urllib.request.Request(
        "%s/dist/%s" % (base, key),
        data=data,
        method="PUT",
        headers={
            "x-token": token,
            "content-type": "application/octet-stream",
            "user-agent": "tv-keep-push/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": True, "raw": body[:200]}


def push_via_wrangler(files: list, dist: str) -> int:
    """备用通道：走 Cloudflare 的 API（wrangler），不需要 worker 的域名能访问。

    有些网络会把 *.workers.dev 挡掉，但 api.cloudflare.com 是通的。
    这条通道不校验 UPLOAD_TOKEN，用的是 wrangler 的登录凭据，
    所以要求这台机器之前 `npx wrangler login` 过。
    """
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("[推送] 没找到 npx。备用通道需要 Node，装一个或改用直连。", file=sys.stderr)
        return 1

    items = []
    for key, full in files:
        with open(full, "r", encoding="utf-8") as f:
            items.append({"key": key, "value": f.read()})
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
        _ = f.read()  # 只是确认配置文件在，失败也不用管

    items.append({
        "key": "_meta",
        "value": json.dumps({
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": [{"path": k, "bytes": os.path.getsize(p)} for k, p in files],
        }, ensure_ascii=False),
    })

    fd, manifest = tempfile.mkstemp(suffix="-tvkeep.json")
    os.close(fd)
    try:
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        print("[推送] 走 Cloudflare API 通道，上传 %d 个键…" % len(items))
        proc = subprocess.run(
            [npx, "wrangler", "kv", "bulk", "put", manifest, "--binding", "TVKEEP", "--remote"],
            cwd=HERE, capture_output=True, text=True, timeout=600,
            shell=(os.name == "nt"),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 or "Success" not in out:
            tail = "\n".join([l for l in out.strip().splitlines() if "Proxy environment" not in l][-8:])
            print("[推送] API 通道失败：\n%s" % tail, file=sys.stderr)
            return 1
        print("[推送] API 通道成功，%d 个键已写入云端。" % len(items))
        return 0
    finally:
        try:
            os.remove(manifest)
        except OSError:
            pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只列出要推的文件，不发请求")
    ap.add_argument("--force", action="store_true", help="忽略哈希，全量重推")
    ap.add_argument("--dist", default="", help="产物目录，默认 ../dist")
    ap.add_argument("--worker", default="", help="临时指定 Worker 地址，覆盖配置文件")
    ap.add_argument("--via", choices=["http", "wrangler"], default="http",
                    help="http=直连 worker 域名（默认）；wrangler=走 Cloudflare API，适合 workers.dev 被挡的网络")
    args = ap.parse_args()

    conf = load_conf()
    base = (args.worker or conf["worker_url"]).rstrip("/")
    dist = args.dist or conf["dist"]
    token = load_token()

    if not os.path.isdir(dist):
        print("[推送] 找不到产物目录：%s" % dist, file=sys.stderr)
        print("       先跑一次 build_live.py / build_vod.py 生成本地产物。", file=sys.stderr)
        return 1

    files = walk(dist)
    if not files:
        print("[推送] 产物目录是空的：%s" % dist, file=sys.stderr)
        return 1

    # 上次推送记录：path -> md5
    prev = {}
    if os.path.exists(STATE) and not args.force:
        try:
            with open(STATE, "r", encoding="utf-8") as f:
                prev = (json.load(f) or {}).get("files", {})
        except Exception:  # noqa: BLE001
            prev = {}

    todo, skipped, hashes = [], [], {}
    for key, full in files:
        h = md5(full)
        hashes[key] = h
        if prev.get(key) == h and not args.force:
            skipped.append(key)
        else:
            todo.append((key, full))

    print("[推送] 共 %d 个文件，需上传 %d 个，跳过未变的 %d 个" % (len(files), len(todo), len(skipped)))

    if args.dry:
        for key, full in todo:
            print("   + %-46s %7.1f KB" % (key, os.path.getsize(full) / 1024))
        print("[推送] --dry 模式，未发送任何请求")
        return 0

    if args.via == "wrangler":
        rc = push_via_wrangler(files, dist)
        if rc == 0:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "files": hashes},
                          f, ensure_ascii=False, indent=1)
            if base:
                print("[推送] 地址不变：%s/dist/multi.json" % base)
        return rc

    if not base:
        print("[推送] 还没配 Worker 地址。", file=sys.stderr)
        print("       在 cf/config.json 里填 {\"worker_url\": \"https://tv-keep.xxx.workers.dev\"}", file=sys.stderr)
        return 1
    if not token:
        print("[推送] 没有 token。把 Worker 里设的那个值写进 cf/.token 文件，", file=sys.stderr)
        print("       或设环境变量 TVKEEP_TOKEN。", file=sys.stderr)
        return 1

    t0 = time.time()
    ok, failed = [], []

    def one(item):
        key, full = item
        with open(full, "rb") as f:
            data = f.read()
        try:
            r = put(base, token, key, data)
            return key, bool(r.get("ok")), r.get("error", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            return key, False, "HTTP %s %s" % (e.code, detail)
        except Exception as e:  # noqa: BLE001
            return key, False, str(e)[:160]

    # 4 并发：KV 对同一个 key 每秒只能写一次，并发太高会白白失败
    with ThreadPoolExecutor(max_workers=4) as ex:
        for key, good, err in ex.map(one, todo):
            (ok if good else failed).append((key, err))
            mark = "✓" if good else "✗"
            print("   %s %s%s" % (mark, key, ("  " + err) if err else ""))

    # 全部成功后写一份索引，状态页和 /health 都靠它
    if not failed:
        meta = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "files": [
                {"path": k, "bytes": os.path.getsize(os.path.join(dist, k.replace("/", os.sep)))}
                for k, _ in files
            ],
        }
        try:
            put(base, token, "_meta", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("[推送] 索引写入失败（不影响产物本身）：%s" % e, file=sys.stderr)

        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"updated": meta["updated"], "files": hashes}, f, ensure_ascii=False, indent=1)

    dt = time.time() - t0
    print("[推送] 完成：成功 %d，失败 %d，用时 %.1fs" % (len(ok), len(failed), dt))
    if failed:
        print("[推送] 失败的文件：%s" % ", ".join(k for k, _ in failed[:8]), file=sys.stderr)
        return 1
    print("[推送] 地址不变，电视端无需任何操作：%s/dist/multi.json" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
