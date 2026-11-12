# -*- coding: utf-8 -*-
"""点播侧：拉上游配置 → 递归展开多仓 → 合并去重 → 逐站真探测 → 剔除死站 → 输出固定地址。

三个关键设计：
1. 多仓每条线路单独产出一份 json，**原样保留该上游自己的 spider / parses**。
   带 jar 爬虫的源丢了 spider 就全废，这是普通"合并去重"工具最大的坑。
2. 多仓会嵌套（线路里还是多仓），所以递归展开，并限制层数与总数，防配置互相引用跑飞。
3. 同一个 API 全局只探一次，多个上游共用结果——否则几千站点跑不完。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLUG = re.compile(r"[^0-9A-Za-z]+")
KEEP_FIELDS = ("searchable", "quickSearch", "filterable", "playerType", "ext",
               "jar", "style", "timeout", "changeable")
BODY_FIELDS = ("spider", "walls", "logo", "sites", "parses", "flags", "lives",
               "wallpaper", "ijk", "doh", "proxy", "headers", "danmaku", "rules", "ads",
               "vars")

DEFAULTS = {
    "probe_mode": "geo_safe",
    "workers": 20,
    "timeout": 10,
    "keep_sites": 90,
    "max_per_upstream": 45,
    "max_repos": 40,
    "max_lines": 20,
    "max_expand_depth": 2,
    "max_probe_sites": 600,
    "base_url": "",
    "lives": [],
    "flags": [],
    "parses": [],
    "min_keep_sites": 5,
}


def load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            default.update(json.load(f))
    return default


def slugify(name: str) -> str:
    """文件名只用 ASCII，避免电视端不认百分号编码；带稳定哈希，增删源也不会串位。"""
    stem = SLUG.sub("-", name).strip("-").lower()[:28]
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (stem, h) if stem else "r-%s" % h


def short_name(label: str, n: int) -> str:
    """多仓条目名只保留能区分的那一段。上游名字是"多仓名·分组·线路名"，
    前缀全都一样，40 条堆在电视上根本分不清，还会被壳按宽度截成乱码。"""
    s = re.sub(r"\s+", " ", label).strip().replace("┃", "·")
    parts = [p.strip() for p in s.split("·") if p.strip()]
    if parts:
        s = parts[-1]
        if len(s) < 4 and len(parts) > 1:
            s = "·".join(parts[-2:])
    if len(s) > 24:
        s = s[:23] + "…"
    return "%s（%d站）" % (s, n)


def join_q(url: str, q: str) -> str:
    return url + ("&" if "?" in url else "?") + q


def guess_base() -> str:
    """本地跑的时候没人给 base_url，就自己推断一个。

    优先级：cf/config.json 的 public_base → worker_url → git remote 推断。
    都推不出来就返回空——那只是意味着配置里不内嵌直播源，不影响其他部分。
    """
    # 这一步决定 multi.json 里每条线路指向哪里，指错了电视上每条线路都是 404。
    try:
        cf = os.path.join(ROOT, "cf", "config.json")
        if os.path.exists(cf):
            with open(cf, "r", encoding="utf-8-sig") as f:
                conf = json.load(f) or {}
            # public_base 已经是完整的 .../dist，直接可用
            base = (conf.get("public_base") or "").strip().rstrip("/")
            if base:
                return base
            url = (conf.get("worker_url") or "").strip().rstrip("/")
            if url:
                return url + "/dist"
    except Exception:  # noqa: BLE001
        pass

    try:
        out = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", out)
    if not m:
        return ""
    return "https://%s.github.io/%s/dist" % (m.group(1).lower(), m.group(2))


def load_sources(path: str) -> list[tuple[str, str]]:
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                label, url = [p.strip() for p in line.split("|", 1)]
            else:
                label, url = "上游%d" % (len(out) + 1), line
            if url.lower().startswith(("http://", "https://")):
                out.append((label, url))
    return out


# ---------- 拉取与展开 ----------

def fetch_cfg(label: str, url: str, timeout: float) -> dict | None:
    try:
        text, code, ms, used = net.fetch_text_best(url, timeout=timeout)
        text = text.lstrip("\ufeff").strip()
        if not text.startswith("{"):
            i = text.find("{")
            if i < 0:
                raise ValueError("返回的不是 JSON（多半是登录页/防盗链/HTML 错误页）")
            text = text[i:]
        cfg = json.loads(text)
        if not isinstance(cfg, dict):
            raise ValueError("顶层不是对象")
        return cfg
    except json.JSONDecodeError as e:
        print("  ! %s 拉取失败：JSON 解析错误 %s" % (label, e))
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, ValueError) else net._short(e)
        print("  ! %s 拉取失败：%s" % (label, msg))
    return None


def clean_sites(sites: list) -> list[dict]:
    """字段瘦身 + 按 (api, type, ext) 去重 + 保证 key 唯一（key 重复会让 TVBox 历史记录错乱）。"""
    out, seen_key, seen_sig = [], set(), set()
    for s in sites:
        if not isinstance(s, dict):
            continue
        api = (s.get("api") or "").strip()
        name = str(s.get("name") or "").strip()
        if not api or not name:
            continue
        typ = str(s.get("type", "1"))
        sig = (api.lower(), typ, str(s.get("ext") or ""))
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        key = str(s.get("key") or "").strip() or re.sub(r"[^0-9A-Za-z]+", "", name) or ("s%d" % len(out))
        stem, i = key, 2
        while key in seen_key:
            key = "%s_%d" % (stem, i)
            i += 1
        seen_key.add(key)
        item = {"key": key, "name": name, "type": s.get("type", "1"), "api": api}
        for f in KEEP_FIELDS:
            if s.get(f) not in ("", None):
                item[f] = s[f]
        out.append(item)
    return out


def collect_repos(sources, cfg) -> list[dict]:
    """递归展开多仓，返回 [{label,url,cfg,sites}]"""
    repos, seen = [], set()
    warned = {"cap": False}

    def push(label: str, url: str, c: dict | None, depth: int) -> None:
        c = c if isinstance(c, dict) else {}
        subs = c.get("urls")
        if isinstance(subs, list) and subs and depth < cfg["max_expand_depth"]:
            targets = []
            for i, ent in enumerate(subs):
                if len(repos) + len(targets) >= cfg["max_repos"]:
                    if not warned["cap"]:
                        print("  · 已达线路上限 %d，多余线路不再展开" % cfg["max_repos"])
                        warned["cap"] = True
                    break
                if isinstance(ent, str):
                    name, u = "线路%d" % (i + 1), ent
                else:
                    name = str(ent.get("name") or "线路%d" % (i + 1))
                    u = str(ent.get("url") or "")
                u = urljoin(url, u.strip())
                if not u.lower().startswith(("http://", "https://")) or u in seen:
                    continue
                seen.add(u)
                targets.append(("%s·%s" % (label, name), u))
            if targets:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    fetched = list(ex.map(lambda t: fetch_cfg(t[0], t[1], cfg["timeout"]), targets))
                for (t_label, t_url), c2 in zip(targets, fetched):
                    push(t_label, t_url, c2, depth + 1)
            return

        sites = clean_sites(c.get("sites") or [])
        if sites:
            repos.append({"label": label, "url": url, "cfg": c, "sites": sites})

    for label, url in sources:
        if url in seen:
            continue
        seen.add(url)
        push(label, url, fetch_cfg(label, url, cfg["timeout"]), 0)
    return repos


# ---------- 探测 ----------

def site_probe(api: str, typ: str, timeout: float) -> dict:
    """返回 {ok, cls, ms, note, need_spider}

    cls: alive（确认可用）/ suspect（可能只是地域限制或反爬）/ dead（确实没了）/ spider（需 jar）
    """
    res = {"ok": False, "cls": "dead", "ms": None, "note": "", "need_spider": False}
    if api.startswith("csp_"):
        res.update(cls="spider", need_spider=True, note="依赖 jar 爬虫，云端无法验证")
        return res
    if api.startswith(("file:", "./", "../")):
        res["note"] = "本地相对路径，云端不可用"
        return res
    if not api.lower().startswith(("http://", "https://")):
        res["note"] = "非 http(s) 地址"
        return res

    low = api.lower().split("?")[0]
    t0 = time.time()
    try:
        if typ == "0" or low.endswith(".xml"):
            raw, _, _ = net.fetch_bytes(api, timeout=timeout, limit=200_000)
            head = raw[:2000].lower()
            if b"<rss" in head or b"<?xml" in head or b"<list" in head:
                res.update(ok=True, cls="alive", note="XML 正常")
            else:
                res.update(cls="suspect", note="返回的不是 XML")
        elif typ == "3" or low.endswith(".js"):
            # js 源本身就是一段脚本，去请求 ac=list 没有意义，只看脚本能不能下载下来
            raw, _, _ = net.fetch_bytes(api, timeout=timeout, limit=600_000)
            if len(raw) > 500:
                res.update(ok=True, cls="alive", note="JS 脚本正常 %d KB" % max(len(raw) // 1024, 1))
            else:
                res.update(cls="suspect", note="脚本内容过短")
        else:
            note, cls = "", "dead"
            for q in ("ac=list", "ac=videolist&pg=1"):
                try:
                    raw, _, _ = net.fetch_bytes(join_q(api, q), timeout=timeout, limit=300_000)
                    data = json.loads(raw.decode("utf-8", "replace").lstrip("\ufeff"))
                    if isinstance(data, dict) and any(k in data for k in ("class", "list", "vod")):
                        res.update(ok=True, cls="alive", note="JSON 采集正常")
                        break
                    note, cls = "JSON 里没有 class/list", "suspect"
                except json.JSONDecodeError:
                    note, cls = "返回的不是 JSON（多半是 HTML 错误页或防盗链）", "suspect"
                except Exception as e:  # noqa: BLE001
                    cls, note = net.classify_error(e)
                    if cls == "dead":
                        break
            res["note"], res["cls"] = note, cls
        res["ms"] = round((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        res["cls"], res["note"] = net.classify_error(e)
        res["ms"] = round((time.time() - t0) * 1000)
    return res


def probe_all(sites: list[dict], cfg: dict) -> None:
    """同一个 (api,type) 全局只探一次，结果回填到所有引用它的站点。"""
    uniq: dict[tuple, dict] = {}
    for s in sites:
        sig = (s["api"].lower(), str(s.get("type", "1")))
        s["_sig"] = sig
        uniq.setdefault(sig, s)

    todo = list(uniq.values())
    capped = False
    if len(todo) > cfg["max_probe_sites"]:
        todo, capped = todo[: cfg["max_probe_sites"]], True
        print("  · 独立 API 共 %d 个，超出上限 %d，只探前 %d 个"
              % (len(uniq), cfg["max_probe_sites"], len(todo)))

    cache: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = [ex.submit(site_probe, s["api"], str(s.get("type", "1")), cfg["timeout"]) for s in todo]
        for s, fut in zip(todo, futs):
            cache[s["_sig"]] = fut.result()

    for s in sites:
        r = cache.get(s["_sig"]) or {"ok": False, "cls": "suspect", "ms": None,
                                     "need_spider": s["api"].startswith("csp_"),
                                     "note": "超出本次探测上限，未验证"}
        s["_ok"], s["_ms"] = r["ok"], r["ms"]
        s["_cls"] = r.get("cls") or ("spider" if r["need_spider"] else "dead")
        s["_note"], s["_spider"] = r["note"], r["need_spider"]
    if capped:
        print("  · 未验证的站点按疑点处理，排在可用站点之后")


# ---------- 主流程 ----------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--vod-dir", default=HERE)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--strict", action="store_true", help="严格模式：只留探测确认可用的站，丢弃疑点")
    args = ap.parse_args()

    cfg = load_json(os.path.join(args.vod_dir, "config.json"), dict(DEFAULTS))
    base = (cfg.get("base_url") or os.environ.get("TVKEEP_BASE_URL")
            or guess_base()).rstrip("/")
    if base:
        print("[点播] 地址前缀：%s" % base)
    else:
        print("[点播] 地址前缀：未设置 —— multi.json 里会写成相对路径 ./repo/xxx.json")
        print("       大部分客户端能解析，但有的不行，那样每条线路都会空。")
        print("       建议在 cf/config.json 填好 worker_url（Cloudflare）或让仓库有 git remote（GitHub），再跑一次。")

    lives = cfg.get("lives") or []
    if not lives and base:
        lives = [{"name": "tv-keep 直播", "url": "%s/live.m3u" % base, "epg": "", "logo": "", "ua": ""}]

    sources = load_sources(os.path.join(args.vod_dir, "sources.txt"))
    print("[点播] %d 个上游配置" % len(sources))

    repos = collect_repos(sources, cfg)
    if not repos:
        print("[失败] 所有上游都没拉到有效站点，本次不覆盖已有结果", file=sys.stderr)
        return 1

    all_sites = [s for rp in repos for s in rp["sites"]]
    for rp in repos:
        for s in rp["sites"]:
            s["_repo"] = rp["label"]
    uniq_api = len({(s["api"].lower(), str(s.get("type", "1"))) for s in all_sites})
    print("[点播] %d 条线路，%d 个站点（独立 API %d 个），开始探活"
          % (len(repos), len(all_sites), uniq_api))

    mode = str(cfg.get("probe_mode") or "geo_safe").lower()
    if args.strict:
        mode = "strict"
    if mode not in ("off", "geo_safe", "strict"):
        print("[警告] probe_mode=%r 不认识，按 geo_safe 处理" % cfg.get("probe_mode"))
        mode = "geo_safe"

    if mode == "off":
        for s in all_sites:
            spider = s["api"].startswith("csp_")
            s.update(_ok=True, _ms=None, _spider=spider,
                     _cls="spider" if spider else "suspect", _note="按要求未探测")
    else:
        probe_all(all_sites, cfg)

    alive = [s for s in all_sites if s["_cls"] == "alive" and not s["_spider"]]
    unsure = [s for s in all_sites if s["_cls"] == "suspect" and not s["_spider"]]
    print("[点播] 模式 %s：确认可用 %d 个，疑点 %d 个，需 jar %d 个"
          % (mode, len(alive), len(unsure), sum(1 for s in all_sites if s["_spider"])))

    # 每条线路各出一份，保留自己的 spider
    repos_out, upstream_log = [], []
    for rp in repos:
        good = [s for s in rp["sites"] if s["_cls"] == "alive"]
        good.sort(key=lambda s: (s["_ms"] if s["_ms"] is not None else 99999))
        good = good[: cfg["max_per_upstream"]]
        spiders = [s for s in rp["sites"] if s["_spider"]][: cfg["max_per_upstream"] * 3]
        extra = []
        if mode == "geo_safe":
            # 疑点站是"境外测不通、但国内可能好好的"，放在最后补位，不挤掉可靠站点
            extra = [s for s in rp["sites"] if s["_cls"] == "suspect" and not s["_spider"]]
            extra.sort(key=lambda s: s["name"])
            extra = extra[: max(0, cfg["max_per_upstream"] - len(good))]
        kept = good + spiders + extra
        if not kept:
            continue
        body = {k: v for k, v in rp["cfg"].items() if k in BODY_FIELDS}
        body["sites"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in kept]
        body["parses"] = rp["cfg"].get("parses") or cfg["parses"]
        body["flags"] = rp["cfg"].get("flags") or cfg["flags"]

        merged = list(lives)
        seen_l = {x.get("url") for x in merged}
        for x in (rp["cfg"].get("lives") or []):
            if isinstance(x, dict) and x.get("url") and x["url"] not in seen_l:
                merged.append(x)
                seen_l.add(x["url"])
        body["lives"] = merged

        repos_out.append({"slug": slugify(rp["label"]), "label": rp["label"], "body": body, "n": len(kept)})
        upstream_log.append({"线路": rp["label"], "原有": len(rp["sites"]), "存活": len(good),
                             "疑点": len(extra), "采用": len(kept), "jar": len(spiders)})

    # ---- 单仓 ----
    # 单仓必须自带 spider，否则所有 csp_ 站（要 jar 才能跑的那种，占七成以上）
    # 一条都加载不出来；也不能只放探测通过的 py 源——影视仓/宝盒对 type:3 的
    # .py 支持很差，表现就是"配置导入成功，但点进去没内容"。
    # 所以挑 jar 站最多的那条上游作基底，连它的 spider 一起继承，再拿验证过的站补位。
    no_spider = sorted(alive, key=lambda s: (s["_ms"] if s["_ms"] is not None else 99999))
    base_body, jar_sites = {}, []
    if repos_out:
        pick = max(repos_out, key=lambda r: sum(
            1 for s in r["body"]["sites"] if str(s.get("api", "")).startswith("csp_")))
        base_body = pick["body"]
        jar_sites = [dict(s, _cls="spider", _ok=True, _ms=None, _note="需 jar")
                     for s in pick["body"]["sites"]
                     if str(s.get("api", "")).startswith("csp_")]
    seen_api = {(s["api"].lower(), str(s.get("type", "1"))) for s in jar_sites}
    rest = [s for s in no_spider if (s["api"].lower(), str(s.get("type", "1"))) not in seen_api]
    # jar 站占七成，剩下的留给真正探测通过的源（4K 直连那类），两边都要有
    jar_quota = min(len(jar_sites), int(cfg["keep_sites"] * 0.7))
    chosen = jar_sites[:jar_quota] + rest[: max(0, cfg["keep_sites"] - jar_quota)]
    if mode == "geo_safe" and len(chosen) < cfg["keep_sites"]:
        fill = sorted(unsure, key=lambda s: s["name"])[: cfg["keep_sites"] - len(chosen)]
        chosen = chosen + [s for s in fill
                           if (s["api"].lower(), str(s.get("type", "1"))) not in seen_api]
    single = {
        "sites": [{k: v for k, v in s.items() if not k.startswith("_")} for s in chosen],
        "lives": lives,
        "parses": base_body.get("parses") or cfg["parses"],
        "flags": base_body.get("flags") or cfg["flags"],
        "wallpaper": "",
    }
    if base_body.get("spider"):
        single["spider"] = base_body["spider"]
    n_jar = sum(1 for s in chosen if str(s.get("api", "")).startswith("csp_"))
    print("[点播] 单仓 %d 个站（其中需 jar %d 个），多仓 %d 条线路"
          % (len(single["sites"]), n_jar, len(repos_out)))

    out = args.out
    prev_n = read_prev_sites(os.path.join(out, "vod-status.json"))
    if prev_n >= 8 and len(single["sites"]) < prev_n * 0.6:
        print("[保护] 上次有 %d 个可用站点，这次只剩 %d 个，跌幅过大，不覆盖已有结果。"
              % (prev_n, len(single["sites"])), file=sys.stderr)
        print("       常见原因：换了网络环境（在境外机器上跑）、上游集体失效。"
              "确认无误后删掉 dist/vod-status.json 再跑。", file=sys.stderr)
        return 2

    if len(single["sites"]) < cfg["min_keep_sites"]:
        print("[失败] 存活站点 %d 少于阈值 %d，本次不覆盖已有结果（宁可留旧结果，也不给你废配置）"
              % (len(single["sites"]), cfg["min_keep_sites"]), file=sys.stderr)
        return 1
    if args.dry:
        return 0

    repo_dir = os.path.join(out, "repo")
    os.makedirs(repo_dir, exist_ok=True)

    urls = []
    for r in repos_out:
        dump(os.path.join(repo_dir, r["slug"] + ".json"), r["body"])
        urls.append({"name": short_name(r["label"], r["n"]),
                     "url": ("%s/repo/%s.json" % (base, r["slug"])) if base else "./repo/%s.json" % r["slug"]})

    # 条目别太多：电视端会把整份索引拉一遍，条目越多启动越慢；而且有些壳
    # 会把所有子仓的 sites 合并成一个列表，超上限就直接白屏。
    urls = urls[: int(cfg.get("max_lines") or 20)]

    # 顶层也带一份 spider/parses/lives —— 有的壳只读多仓索引的顶层字段，
    # 切进子仓后不再加载子仓的 spider，缺了就是"能选线路，进去没内容"。
    multi = {"urls": urls}
    if base_body.get("spider"):
        multi["spider"] = base_body["spider"]
    if single.get("parses"):
        multi["parses"] = single["parses"]
    if single.get("lives"):
        multi["lives"] = single["lives"]
    if single.get("flags"):
        multi["flags"] = single["flags"]

    dump(os.path.join(out, "tvbox.json"), single)
    dump(os.path.join(out, "multi.json"), multi)
    write_report(os.path.join(out, "vod-report.md"), chosen, upstream_log, all_sites, cfg, base, mode)
    write_status(os.path.join(out, "vod-status.json"), len(single["sites"]), len(repos_out), upstream_log)
    print("[写入] tvbox.json / multi.json / repo/*.json / vod-report.md")
    return 0


def dump(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def write_report(path, kept, upstream_log, all_sites, cfg, base, mode="geo_safe") -> None:
    dead = [s for s in all_sites if s["_cls"] == "dead" and not s["_spider"]]
    dead.sort(key=lambda s: (s["_repo"], s["name"]))
    out = [
        "# 点播体检报告",
        "",
        "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "**单仓可用站点 %d 个**；多仓 %d 条线路。地址前缀：`%s`"
        % (len(kept), len(upstream_log), base or "未设置（相对路径）"),
        "",
        "探测模式：`%s`。%s" % (mode, {
            "geo_safe": "只剔除确定已死的站点（域名没了、404、连接被拒）；"
                        "403 和超时会被当成疑点保留，因为在国内往往能正常打开。",
            "strict": "只保留探测确认可用的站点，误杀率更高但配置更干净。",
            "off": "不做任何网络验证，只合并去重。",
        }.get(mode, "")),
        "",
        "## 各线路存活情况",
        "",
        "| 线路 | 原有 | 确认可用 | 疑点 | jar 站 | 采用 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for u in upstream_log:
        out.append("| %s | %d | %d | %d | %d | %d |"
                   % (u["线路"], u["原有"], u["存活"], u["疑点"], u["jar"], u["采用"]))
    out += ["", "## 本次剔除的站点（确认已死）", "", "| 线路 | 站点 | 原因 |", "| --- | --- | --- |"]
    for s in dead[:150]:
        out.append("| %s | %s | %s |" % (s["_repo"], s["name"], s["_note"]))
    if len(dead) > 150:
        out.append("| … | 还有 %d 个 | 同上 |" % (len(dead) - 150))
    out += ["", "## 单仓采用站点", "", "| 站点 | 类型 | 状态 | 延迟 | 备注 |",
            "| --- | --- | --- | --- | --- |"]
    for s in kept:
        out.append("| %s | %s | %s | %s | %s |" % (
            s.get("name", "?"), s.get("type", "?"),
            {"alive": "可用", "suspect": "疑点", "spider": "可用（需 jar）"}.get(
                s.get("_cls"), s.get("_cls", "?")),
            s.get("_ms"), s.get("_note", "")))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def read_prev_sites(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("single_sites", 0))
    except Exception:  # noqa: BLE001
        return 0


def write_status(path, single_count, repo_count, upstream_log) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "single_sites": single_count, "repos": repo_count,
                   "upstreams": {u["线路"]: u["存活"] for u in upstream_log}},
                  f, ensure_ascii=False, indent=1)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
