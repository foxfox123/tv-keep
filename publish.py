#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 dist/ 推到 GitHub，清掉 jsDelivr 缓存，并验证线上真的变成了新内容。

两个关键点，都是实测踩出来的：

1. 地址要用「不带 @branch」的写法
   https://cdn.jsdelivr.net/gh/user/repo/dist/xxx
   而不是 .../gh/user/repo@main/dist/xxx
   后者有独立缓存，清了也常常还是旧内容（实测：@main 一直返回旧版，
   无版本写法三个节点立刻都是新版）。

2. 清缓存只是"请求"，不保证成功
   jsDelivr 各边缘节点状态不一致。所以推完必须去线上核对，
   对不上就再刷一次，而不是在本地打印一句"成功"了事。
"""
import argparse
import json
import os
import random
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
UA = "Mozilla/5.0 (tv-keep)"


def sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def load_conf() -> dict:
    conf = {"public_base": ""}
    if os.path.exists(CONF):
        try:
            with open(CONF, "r", encoding="utf-8-sig") as f:
                conf.update(json.load(f) or {})
        except Exception:  # noqa: BLE001
            pass
    return conf


def jd_parts(base: str) -> tuple[str, str, str]:
    """从 public_base 解析 user / repo / branch。

    https://cdn.jsdelivr.net/gh/foxfox123/tv-keep/dist          -> ('foxfox123','tv-keep','')
    https://cdn.jsdelivr.net/gh/foxfox123/tv-keep@main/dist    -> ('foxfox123','tv-keep','main')
    """
    base = (base or "").strip().rstrip("/")
    if "/gh/" not in base:
        return "", "", ""
    seg = urllib.parse.urlparse(base).path.split("/gh/", 1)[1].split("/")
    if len(seg) < 2:
        return "", "", ""
    repo, _, branch = seg[1].partition("@")
    return seg[0], repo, branch


def purge_path(user: str, repo: str, branch: str, path: str) -> str:
    spec = "%s/%s" % (user, repo) if not branch else "%s/%s@%s" % (user, repo, branch)
    return "/gh/%s/%s" % (spec, path.lstrip("/"))


def purge_one(path: str, user: str, repo: str, branch: str) -> tuple[str, bool, str]:
    p = purge_path(user, repo, branch, path)
    req = urllib.request.Request(PURGE + p,
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip().startswith("{") else {}
            info = (data.get("paths") or {}).get(p) or {}
            if info.get("throttled"):
                return path, False, "限流"
            prov = info.get("providers") or {}
            ok = r.status == 200 and bool(prov) and all(prov.values())
            return path, ok, "" if ok else "节点未全部刷新"
    except urllib.error.HTTPError as e:
        return path, False, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001
        return path, False, str(e)[:60]


def fetch_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-purge", action="store_true", help="只推送，不清缓存")
    ap.add_argument("--no-push", action="store_true", help="只提交到本地，不推送")
    ap.add_argument("--message", default="", help="自定义提交信息")
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"))
    ap.add_argument("--verify-tries", type=int, default=4, help="线上校验最大轮数")
    args = ap.parse_args()

    conf = load_conf()
    base = (conf.get("public_base") or "").strip().rstrip("/")
    user, repo, branch = jd_parts(base)
    if not user:
        print("[发布] cf/config.json 的 public_base 没填或格式不对。", file=sys.stderr)
        print("       形如 https://cdn.jsdelivr.net/gh/用户名/仓库名/dist", file=sys.stderr)
        return 2

    rel_dist = os.path.relpath(args.dist, HERE).replace(os.sep, "/")

    # 1) 版本标记：让"线上是否已更新"可被程序判断，而不是靠人眼
    token = "%s-%04x" % (time.strftime("%Y%m%d%H%M%S"), random.randint(0, 0xFFFF))
    try:
        os.makedirs(args.dist, exist_ok=True)
        with open(os.path.join(args.dist, "version.json"), "w", encoding="utf-8") as f:
            json.dump({"token": token, "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        print("[发布] 写版本标记失败：%s" % e, file=sys.stderr)

    # 2) 提交
    if sh(["git", "rev-parse", "--is-inside-work-tree"]).returncode != 0:
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
        changed = [ln.strip() for ln in
                   sh(["git", "show", "--name-only", "--format=", "HEAD"]).stdout.splitlines() if ln.strip()]
        print("[发布] 已提交 %d 个文件" % len(changed))
    else:
        changed = []
        print("[发布] 内容没有变化，跳过提交")

    # 3) 推送
    if not args.no_push:
        ref = "HEAD:%s" % (branch or "main")
        p = sh(["git", "push", "origin", ref])
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            # 远端被别人推过（比如仓库里那个每日 Actions）就会 non-fast-forward。
            # 这里先 rebase 再重试一次，否则这台机器从此永远推不上去。
            if "rejected" in err or "fetch first" in err or "non-fast-forward" in err:
                print("[发布] 远端有新提交，先合并再推一次")
                r = sh(["git", "pull", "--rebase", "--autostash", "origin", branch or "main"])
                if r.returncode == 0:
                    p = sh(["git", "push", "origin", ref])
                    err = (p.stderr or p.stdout).strip()
        if p.returncode != 0:
            print("[发布] 推送失败：", file=sys.stderr)
            print(err[:600], file=sys.stderr)
            if "Permission denied" in err or "publickey" in err:
                print("\n  这台机器的密钥没被 GitHub 认可。要用的那把是：", file=sys.stderr)
                print("  ~/.ssh/github_tvkeep_rsa", file=sys.stderr)
            return 2
        print("[发布] 已推送到 github.com/%s/%s (%s)" % (user, repo, branch or "默认分支"))

    if args.no_push:
        return 0

    # 4) 清缓存：只清这次真改过的、电视会读的文件
    rel_changed = []
    for path in changed:
        if not path.startswith(rel_dist + "/"):
            continue
        rel = path[len(rel_dist) + 1:]
        if rel.endswith("-hist.json") or rel.endswith("-status.json"):
            continue
        # repo/*.json 是每条线路的配置，缓存清了也没用（实测 purge 无效），
        # 清它们只会触发 jsDelivr 限流，日志里一片红。只清"入口文件"。
        if rel.startswith("repo/"):
            continue
        rel_changed.append(rel)

    if not args.no_purge and rel_changed:
        rel_changed.sort(key=lambda p: (0 if p == "multi.json" else 1, p))
        print("[发布] 清 jsDelivr 缓存 %d 个文件…" % len(rel_changed))
        with ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(lambda p: purge_one(p, user, repo, branch), rel_changed))
        ok_n = sum(1 for _, ok, _ in res if ok)
        print("[发布] 清缓存请求完成 %d/%d" % (ok_n, len(rel_changed)))
        for p, ok, note in res:
            if not ok:
                print("   · %s 未刷新（%s）" % (p, note))

    # 5) 线上核对：如实报出线上是哪个版本，不谎报"立刻生效"
    #    实测：jsDelivr 的 purge 接口对边缘缓存无效，@main 和无版本写法都一样，
    #    最坏会锁 12 小时（s-maxage=43200）。所以这里只做"如实报告"。
    marker_url = "%s/version.json" % base
    live_token = ""
    for attempt in range(1, args.verify_tries + 1):
        try:
            txt = fetch_text(marker_url, timeout=20)
            if token in txt:
                live_token = token
                break
            m = json.loads(txt)
            live_token = m.get("token") or "未知"
            if attempt < args.verify_tries:
                print("[核对] 线上还是上一个版本（%s），%d 秒后再看…" % (m.get("time") or "?", 8))
                time.sleep(8)
        except Exception as e:  # noqa: BLE001
            print("[核对] 暂时取不到 version.json（%s）" % str(e)[:40])
            break

    print()
    print("电视上填这个地址（永远不用改）：")
    print("  %s/multi.json" % base)
    if live_token == token:
        print("线上已是最新版本。")
    else:
        print("本地版本：%s" % token)
        print("线上版本：%s" % (live_token or "取不到"))
        print("jsDelivr 边缘缓存会滞后：实测通常十几分钟到半小时，最坏可能到 12 小时。")
        print("清缓存接口（purge.jsdelivr.net）对它无效，加 ?t= 也绕不过，这是平台行为。")
        print("配置本身没问题，只是到得晚；不用重跑，也不用改地址。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
