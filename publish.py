#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 dist/ 推到 GitHub，然后清掉 jsDelivr 的 CDN 缓存。

为什么需要清缓存：jsDelivr 对分支地址（@main）的 CDN 缓存是 12 小时
（响应头 s-maxage=43200）。不清的话，电视今天抽到的还是昨天的配置。

只清"这次真的改过"的文件，避免把 40 多个线路全清一遍触发限流。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "cf", "config.json")
PURGE = "https://purge.jsdelivr.net"


def sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def load_conf() -> dict:
    conf = {"public_base": "", "jsdelivr": {}}
    if os.path.exists(CONF):
        try:
            with open(CONF, "r", encoding="utf-8-sig") as f:
                conf.update(json.load(f) or {})
        except Exception:  # noqa: BLE001
            pass
    return conf


def jd_parts(conf: dict) -> tuple[str, str, str]:
    """从 public_base 里解析出 user / repo / branch。

    public_base 形如 https://cdn.jsdelivr.net/gh/foxfox123/tv-keep@main/dist
    """
    base = (conf.get("public_base") or "").strip().rstrip("/")
    if not base:
        return "", "", ""
    p = urllib.parse.urlparse(base)
    if "/gh/" not in p.path:
        return "", "", ""
    seg = p.path.split("/gh/", 1)[1].split("/")
    if len(seg) < 2:
        return "", "", ""
    user = seg[0]
    repo, _, branch = seg[1].partition("@")
    return user, repo, branch or "main"


def purge_one(path: str, user: str, repo: str, branch: str) -> tuple[str, bool, str]:
    url = "%s/gh/%s/%s@%s/%s" % (PURGE, user, repo, branch, path.lstrip("/"))
    req = urllib.request.Request(url, headers={"User-Agent": "tv-keep/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip().startswith("{") else {}
            info = (data.get("paths") or {}).get("/gh/%s/%s@%s/%s" % (user, repo, branch, path.lstrip("/"))) or {}
            throttled = bool(info.get("throttled"))
            ok = r.status == 200 and not throttled
            return path, ok, "限流" if throttled else ""
    except urllib.error.HTTPError as e:
        return path, False, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001
        return path, False, str(e)[:60]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-purge", action="store_true", help="只推送，不清 CDN 缓存")
    ap.add_argument("--no-push", action="store_true", help="只提交到本地，不推送")
    ap.add_argument("--message", default="", help="自定义提交信息")
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"), help="要发布的目录")
    args = ap.parse_args()

    conf = load_conf()
    user, repo, branch = jd_parts(conf)
    if not user:
        print("[发布] cf/config.json 里的 public_base 没填，不知道往哪推。", file=sys.stderr)
        print("       形如 https://cdn.jsdelivr.net/gh/用户名/仓库名@main/dist", file=sys.stderr)
        return 2

    rel_dist = os.path.relpath(args.dist, HERE).replace(os.sep, "/")

    # 1) 暂存 + 提交
    r = sh(["git", "rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0:
        print("[发布] 这里不是 git 仓库：%s" % HERE, file=sys.stderr)
        return 2

    sh(["git", "add", "-A"])
    staged = sh(["git", "status", "--porcelain"]).stdout.strip()
    if staged:
        msg = args.message or ("update: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        c = sh(["git", "commit", "-m", msg])
        if c.returncode != 0:
            print("[发布] 提交失败：%s" % (c.stderr or c.stdout).strip()[:300], file=sys.stderr)
            return 2
        changed = [ln.strip() for ln in sh(["git", "show", "--name-only", "--format=", "HEAD"]).stdout.splitlines() if ln.strip()]
        print("[发布] 已提交 %d 个文件" % len(changed))
    else:
        changed = []
        print("[发布] 内容没有变化，跳过提交")

    # 2) 推送
    if not args.no_push:
        p = sh(["git", "push", "origin", "HEAD:%s" % branch])
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            print("[发布] 推送失败：", file=sys.stderr)
            print(err[:600], file=sys.stderr)
            if "Permission denied" in err or "publickey" in err:
                print("\n  原因：这台机器的 SSH 公钥没有被 GitHub 认可。", file=sys.stderr)
                print("  打开 https://github.com/settings/keys ，添加下面这行公钥：", file=sys.stderr)
                pub = os.path.expanduser("~/.ssh/id_rsa.pub")
                if os.path.exists(pub):
                    print("  " + open(pub, encoding="utf-8").read().strip(), file=sys.stderr)
            return 2
        print("[发布] 已推送到 github.com/%s/%s (%s)" % (user, repo, branch))

    # 3) 清 CDN 缓存：只清这次改过的、且真在 dist 里的文件
    if args.no_purge or args.no_push:
        return 0
    if not changed:
        return 0

    targets = []
    for path in changed:
        if not path.startswith(rel_dist + "/"):
            continue
        rel = path[len(rel_dist) + 1:]
        # 电视只读这几类，报告和状态文件不用清
        if rel.endswith("-hist.json") or rel.endswith("-status.json"):
            continue
        targets.append(rel)

    if not targets:
        return 0

    # multi.json 最先清：它决定每条线路指向哪个文件
    targets.sort(key=lambda p: (0 if p == "multi.json" else 1, p))
    print("[发布] 清 jsDelivr 缓存 %d 个文件…" % len(targets))
    fails = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for path, ok, note in ex.map(lambda p: purge_one(p, user, repo, branch), targets):
            if not ok:
                fails.append((path, note))

    if fails:
        # jsDelivr 拿到新 commit 有几十秒延迟，失败的再试一轮
        print("[发布] %d 个没清成功，等 5 秒重试…" % len(fails))
        time.sleep(5)
        with ThreadPoolExecutor(max_workers=4) as ex:
            again = list(ex.map(lambda t: purge_one(t[0], user, repo, branch), fails))
        fails = [(p, n) for p, ok, n in again if not ok]

    ok_n = len(targets) - len(fails)
    print("[发布] 缓存已清 %d/%d" % (ok_n, len(targets)))
    for p, n in fails[:8]:
        print("   · 未清成功 %s（%s）" % (p, n))
    if fails:
        print("   影响：这几个文件最多晚 12 小时才更新，不影响播放。")

    print("\n电视上填这个地址（永远不用改）：")
    print("  %s/multi.json" % (conf["public_base"].rstrip("/")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
