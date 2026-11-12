# -*- coding: utf-8 -*-
"""直播侧：采集 → 只留你要的台 → 真实测速 → 每台保留 N 条 → 输出固定地址的 m3u/txt。

用法：
    python live/build_live.py            # 用 live/ 下的配置
    python live/build_live.py --dry      # 只采集和探测，不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')

# 已知的牌照方 / 国内 CDN 域名特征。这类地址走正规分发，比个人中转活得久。
DOMESTIC_HINT = ("gitv.tv", "wasu", "voc.com.cn", "cbncloud", "10099", "189.cn",
                 "cmvideo", "chinamobile", "chinaunicom", "telecom", "cdn", "hls")


def host_tier(url: str) -> int:
    """给线路的宿主质量分档，越小越好。

    0 = 域名且像正规 CDN ／ 1 = 普通域名 ／ 2 = 纯 IP 直连 ／ 3 = 解析不出来
    纯 IP 的地址大多是个人搭的中转，在境外测延迟很低，国内反而常常拉不动，
    所以即使在境外跑，也应该让域名线路排在它们前面。
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host:
        return 3
    if re.fullmatch(r"[\d.]+", host):
        return 2
    if any(h in host for h in DOMESTIC_HINT):
        return 0
    return 1


def load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            default.update(json.load(f))
    return default


def load_channels(path: str) -> list[dict]:
    """channels.txt 每行：频道名 | 别名1,别名2 | 分组"""
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            name = parts[0]
            aliases = [a.strip() for a in parts[1].split(",") if a.strip()] if len(parts) > 1 else []
            group = parts[2] if len(parts) > 2 and parts[2] else "其他"
            out.append({"name": name, "aliases": aliases, "group": group})
    return out


def load_sources(path: str) -> list[tuple[str, str]]:
    """sources.txt 每行：备注 | 地址（备注可省略）"""
    out = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                label, url = [p.strip() for p in line.split("|", 1)]
            else:
                label, url = "源%d" % (len(out) + 1), line
            if url.lower().startswith(("http://", "https://")):
                out.append((label, url))
    return out


def parse_m3u(text: str) -> list[dict]:
    items = []
    pending = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            attrs = dict(ATTR.findall(s))
            display = s.rsplit(",", 1)[-1].strip() if "," in s else ""
            pending = {
                "name": display or attrs.get("tvg-name", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", ""),
            }
        elif s.startswith("#"):
            continue
        elif pending is not None:
            pending["url"] = s
            items.append(pending)
            pending = None
    return items


def parse_txt(text: str) -> list[dict]:
    """TVBox / DIYP 的 txt 直播格式：分组,#genre#  然后  频道名,url"""
    items = []
    group = ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if ",#genre#" in s:
            group = s.replace(",#genre#", "").strip()
            continue
        if s.startswith("#") or "," not in s:
            continue
        name, url = s.split(",", 1)
        url = url.strip()
        if url.lower().startswith(("http://", "https://")):
            items.append({"name": name.strip(), "tvg_name": "", "logo": "", "group": group, "url": url})
    return items


def match_score(ch: dict, cand_name: str) -> int:
    """匹配打分：完全一致 > 前缀 > 包含；CCTV-1 不能吃掉 CCTV-11。"""
    n = net.normalize(cand_name)
    if not n:
        return 0
    plus_cand = "+" in cand_name
    for i, alias in enumerate([ch["name"]] + ch["aliases"]):
        an = net.normalize(alias)
        if not an:
            continue
        plus_alias = "+" in alias
        if plus_cand != plus_alias:
            continue
        if n == an:
            return 100 - i
        if n.startswith(an):
            if not n[len(an):][:1].isdigit():
                return 85 - i
        # 源里只写了 "CCTV1"，我们写的是 "CCTV-1 综合"：反向前缀也算命中
        if an.startswith(n) and len(n) >= 4:
            if not an[len(n):][:1].isdigit():
                return 80 - i
        idx = n.find(an)
        if idx >= 0 and len(an) >= 3:
            if not n[idx + len(an):][:1].isdigit():
                return 60 - i
    return 0


def collect(sources: list[tuple[str, str]], timeout: float) -> tuple[list[dict], list[dict]]:
    """并发拉取所有订阅源，返回 (候选列表, 源状态)"""
    cands, status = [], []

    def one(item):
        label, url = item
        t0 = time.time()
        try:
            text, code, ms, used = net.fetch_text_best(url, timeout=timeout)
            items = parse_m3u(text) if "#EXTM3U" in text[:2048].upper() else parse_txt(text)
            note = "HTTP %s" % code
            if used != url:
                note += "（原地址不通，已走镜像）"
            return label, url, items, code, ms, note
        except Exception as e:  # noqa: BLE001
            return label, url, [], 0, (time.time() - t0) * 1000, net._short(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(one, s) for s in sources]):
            label, url, items, code, ms, err = fut.result()
            status.append({"源": label, "状态": "OK" if items else "失败", "条数": len(items),
                           "耗时ms": round(ms), "备注": err or note})
            for it in items:
                it["src"] = label
                cands.append(it)
    return cands, status


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--strict", action="store_true", help="真测速（在国内跑才有意义），覆盖 probe_mode")
    ap.add_argument("--live-dir", default=HERE)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args()

    cfg = load_json(os.path.join(args.live_dir, "config.json"), {
        "probe_mode": "geo_safe",
        "keep_per_channel": 6,
        "workers": 24,
        "probe_timeout": 8,
        "min_kbps": 200,
        "sample_bytes": 200000,
        "max_probe_seconds": 5,
        "min_match_score": 55,
        "epg": "",
        "out_name": "live",
    })
    mode = str(cfg.get("probe_mode") or "geo_safe").lower()
    if args.strict:
        mode = "strict"
    if mode not in ("off", "geo_safe", "strict"):
        print("[警告] probe_mode=%r 不认识，按 geo_safe 处理" % cfg.get("probe_mode"))
        mode = "geo_safe"
    deep = mode == "strict"
    channels = load_channels(os.path.join(args.live_dir, "channels.txt"))
    sources = load_sources(os.path.join(args.live_dir, "sources.txt"))
    print("[采集] %d 个订阅源，目标 %d 个频道" % (len(sources), len(channels)))

    cands, src_status = collect(sources, timeout=30)
    print("[采集] 拿到 %d 条原始线路" % len(cands))

    # 每台先筛出匹配的候选。注意：候选对象必须全局共用一份，
    # 因为探测结果要写回同一个对象；如果每个频道各自复制一份字典，
    # 落在多个频道里的同一条线路就只有第一个频道拿得到结果。
    src_of: dict[str, set[str]] = {}
    canon: dict[str, dict] = {}
    for c in cands:
        src_of.setdefault(c["url"], set()).add(c["src"])
        canon.setdefault(c["url"], c)

    buckets: dict[str, dict[str, int]] = {}
    for ch in channels:
        seen: dict[str, int] = {}
        for c in cands:
            sc = match_score(ch, c.get("tvg_name") or c["name"])
            if sc < cfg["min_match_score"]:
                continue
            if sc > seen.get(c["url"], -1):
                seen[c["url"]] = sc
        buckets[ch["name"]] = seen

    wanted = {u for b in buckets.values() for u in b}
    todo = [canon[u] for u in wanted]
    # 有几个订阅源同时提供了这条线，是"这条线有多主流"的粗略代理
    for c in todo:
        c["nsrc"] = len(src_of.get(c["url"]) or {1})
        c["tier"] = host_tier(c["url"])
    print("[探测] 待测 %d 条线路，并发 %d，模式 %s" % (len(todo), cfg["workers"], mode))

    if mode == "off":
        for c in todo:
            c.update(ok=True, cls="alive", kbps=0, height=0, kind="?", note="按要求未探测", ttfb=None)
    else:
        def probe(c):
            return c, net.probe_stream(
                c["url"], timeout=cfg["probe_timeout"], sample=cfg["sample_bytes"],
                max_seconds=cfg["max_probe_seconds"], deep=deep)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
            for fut in as_completed([ex.submit(probe, c) for c in todo]):
                c, r = fut.result()
                c["ok"], c["cls"], c["kbps"], c["kind"], c["note"], c["ttfb"] = (
                    r["ok"], r["cls"], r["kbps"], r["kind"], r["note"], r["ttfb_ms"])
                c["height"] = r.get("height", 0)

    hist = load_hist(os.path.join(args.out, cfg["out_name"] + "-hist.json"))

    def rank(c: dict):
        """geo_safe 下没有可信的速度，改用"这条线连续活了几天"排序。

        存活天数由每天的运行累积而来，跟测速机器在哪无关，是目前唯一站得住的信号。
        如果哪天你在国内用 --strict 跑过一次，那次测到的速度也会记进历史，一起参与排序。
        """
        h = hist.get(c["url"]) or {}
        days = int(h.get("days", 0))
        kb = int(h.get("kbps", 0))
        tier = c.get("tier", 1)
        if mode == "strict":
            # 国内实测速度是硬指标，速度优先
            return (-c.get("kbps", 0), tier, -days, c.get("ttfb") or 9999, c["url"])
        # 没有可信速度时：先把纯 IP 中转压到后面，再在同类里挑活得最久的
        return (tier, -days, -kb, -c.get("nsrc", 0), c.get("ttfb") or 9999, c["url"])

    result, report = [], []
    for ch in channels:
        pool = [canon[u] for u in buckets[ch["name"]]]
        good = [c for c in pool if c.get("cls") == "alive"]
        if mode == "strict":
            good = [c for c in good if c.get("kbps", 0) >= cfg["min_kbps"]]
        unsure = [c for c in pool if c.get("cls") == "suspect"]
        good.sort(key=rank)
        unsure.sort(key=rank)
        chosen = good[: cfg["keep_per_channel"]]
        # 疑点线路只在可用线路凑不够时补位，且最多补 2 条，避免整份列表被死链占满
        if len(chosen) < cfg["keep_per_channel"]:
            chosen += unsure[: min(2, cfg["keep_per_channel"] - len(chosen))]
        for c in chosen:
            c["channel"] = ch["name"]
            c["group"] = ch["group"]
            result.append(c)
        report.append({
            "频道": ch["name"], "分组": ch["group"], "候选": len(pool),
            "可用": len(good), "疑点": len(unsure), "采用": len(chosen),
            "最佳kbps": good[0]["kbps"] if good and mode == "strict" else 0,
            "最佳延迟ms": good[0].get("ttfb") if good else None,
            "来源": ", ".join(sorted({c["src"] for c in chosen})) if chosen else "—",
        })

    # 把本次实测到的存活情况累积进历史，供明天排序用
    bump_hist(hist, [c for c in todo if c.get("cls") == "alive"])

    # 分组顺序已经由 channels.txt 的遍历顺序保证，这里不再重排。
    # （早先按 kbps 重排过一次，会把已经排好的名次打乱，因为裸流会测出速度、HLS 不会。）

    print("[结果] 可用频道 %d/%d，输出线路 %d 条"
          % (sum(1 for r in report if r["采用"] > 0), len(channels), len(result)))

    now_ok = {r["频道"]: r["采用"] for r in report}
    prev = read_status(os.path.join(args.out, cfg["out_name"] + "-status.json"))
    if prev and not args.dry:
        common = [k for k in now_ok if k in prev]
        prev_n = sum(1 for k in common if prev[k] > 0)
        now_n = sum(1 for k in common if now_ok[k] > 0)
        if prev_n >= 5 and now_n < prev_n * 0.6:
            print("[保护] 上次这些台有 %d 个能用，这次只剩 %d 个，跌幅过大，不覆盖已有结果。"
                  % (prev_n, now_n), file=sys.stderr)
            print("       常见原因：换了网络环境（比如在境外机器上跑）、订阅源集体失效。"
                  "确认无误后可删掉 dist/%s-status.json 再跑。" % cfg["out_name"], file=sys.stderr)
            return 2

    if not args.dry:
        os.makedirs(args.out, exist_ok=True)
        name = cfg["out_name"]
        write_m3u(os.path.join(args.out, name + ".m3u"), result, cfg.get("epg", ""))
        write_txt(os.path.join(args.out, name + ".txt"), result)
        write_report(os.path.join(args.out, name + "-report.md"), report, src_status, cfg, mode)
        write_status(os.path.join(args.out, name + "-status.json"), report)
        save_hist(os.path.join(args.out, name + "-hist.json"), hist, set(canon))
        print("[写入] %s/%s.m3u 与 .txt 与 -report.md" % (args.out, name))

    if not result:
        print("[失败] 没有任何可用线路，本次不覆盖已有结果", file=sys.stderr)
        return 1
    return 0


def write_m3u(path: str, items: list[dict], epg: str) -> None:
    lines = ['#EXTM3U x-tvg-url="%s"' % epg if epg else "#EXTM3U"]
    for it in items:
        logo = ' tvg-logo="%s"' % it.get("logo", "") if it.get("logo") else ""
        lines.append(
            '#EXTINF:-1 tvg-name="%s"%s group-title="%s",%s'
            % (it["channel"], logo, it["group"], it["channel"])
        )
        lines.append(it["url"])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def write_txt(path: str, items: list[dict]) -> None:
    lines = ["# 由 tv-keep 自动生成 · %s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    cur = None
    for it in items:
        if it["group"] != cur:
            cur = it["group"]
            lines.append("%s,#genre#" % cur)
        lines.append("%s,%s" % (it["channel"], it["url"]))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


MODE_NOTE = {
    "strict": "真测速模式：实际拉取分片并测速率，只有国内跑出来的结果才准。",
    "geo_safe": "稳妥模式：只验播放列表，剔除确定已死的线路，速度排序交给历史存活天数。",
    "off": "不探测模式：只做采集与合并去重，完全不做网络验证。",
}


def write_report(path: str, report: list[dict], src_status: list[dict], cfg: dict, mode: str) -> None:
    ok = sum(1 for r in report if r["采用"] > 0)
    out = [
        "# 直播体检报告",
        "",
        "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "**%d/%d 个频道有可用线路**，每台上限 %d 条。探测模式：`%s`"
        % (ok, len(report), cfg["keep_per_channel"], mode),
        "",
        "> %s" % MODE_NOTE.get(mode, ""),
        "",
        "| 频道 | 分组 | 候选 | 可用 | 疑点 | 采用 | 最佳速率 | 最佳延迟 | 来自 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report:
        out.append("| %s | %s | %d | %d | %d | %d | %s | %s | %s |" % (
            r["频道"], r["分组"], r["候选"], r["可用"], r.get("疑点", 0), r["采用"],
            ("%s kbps" % r["最佳kbps"]) if r["最佳kbps"] else "—",
            ("%s ms" % r["最佳延迟ms"]) if r["最佳延迟ms"] else "—", r["来源"]))
    dead = [r["频道"] for r in report if r["采用"] == 0]
    if dead:
        out += ["", "## 今天没找到可用线路的频道", "",
                "这些台要么所有订阅源都没提供，要么提供的全挂了。处理方法：往 `live/sources.txt` 里再加一两个源。",
                "", "、".join(dead)]
    out += ["", "## 订阅源状态", "", "| 源 | 状态 | 条数 | 耗时 | 备注 |", "| --- | --- | --- | --- | --- |"]
    for s in src_status:
        out.append("| %s | %s | %d | %s ms | %s |" % (s["源"], s["状态"], s["条数"], s["耗时ms"], s["备注"] or "—"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def load_hist(path: str) -> dict:
    """历史库：记录每条线路第一次见到的时间、累计存活天数、测到过的速度。

    这份文件随 dist/ 一起提交到仓库，所以排序依据能跨天延续。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("urls") or {}
    except Exception:  # noqa: BLE001
        return {}


def bump_hist(hist: dict, alive: list[dict]) -> None:
    """把本次确认可用的线路记进历史。同一天跑多次只累加一次天数。"""
    today = time.strftime("%Y-%m-%d")
    for c in alive:
        h = hist.setdefault(c["url"], {"first": today, "last": "", "days": 0, "kbps": 0})
        if h.get("last") != today:
            h["days"] = int(h.get("days", 0)) + 1
            h["last"] = today
        if c.get("kbps"):
            h["kbps"] = int(c["kbps"])


def save_hist(path: str, hist: dict, keep_urls: set[str]) -> None:
    """只保留本次还在源里出现的线路，外加 60 天内活过的，避免文件无限膨胀。"""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 60 * 86400))
    pruned = {u: v for u, v in hist.items()
              if u in keep_urls or (v.get("last") or "") >= cutoff}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "urls": pruned},
                  f, ensure_ascii=False, indent=0, sort_keys=True)
        f.write("\n")


def read_status(path: str) -> dict:
    """读取上次的每台可用数，用于判断结果是否暴跌。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: int(v.get("采用", 0)) for k, v in (data.get("channels") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def write_status(path: str, report: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "channels": {r["频道"]: {"可用": r["可用"], "采用": r["采用"]} for r in report}},
                  f, ensure_ascii=False, indent=1)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
