/**
 * tv-keep · Cloudflare Workers 端
 *
 * 职责只有两件：把本机推上来的产物存进 KV，再用一个永不改变的地址发出去。
 * 探测（拉源、测速、探活）不在云端跑——Workers 免费版每次调用只能发 50 个子请求，
 * 而点播侧一次要探近千个站，跑不完；且云端出口在境外，测国内直播必然误判。
 *
 * 部署后需要两项配置：
 *   1) KV 绑定：变量名必须叫 TVKEEP
 *   2) 变量：UPLOAD_TOKEN（类型选 Secret），本机推送时带上同一个值
 *
 * 路由：
 *   GET  /                 状态页（最后更新时间、文件清单、电视该填哪个地址）
 *   GET  /dist/<文件>       返回产物（/multi.json 这种省略 dist/ 的写法也支持）
 *   PUT  /dist/<文件>       上传（需 x-token 头）
 *   GET  /health           给脚本判断用
 */

const MIME = {
  json: "application/json; charset=utf-8",
  m3u: "audio/x-mpegurl; charset=utf-8",
  m3u8: "application/vnd.apple.mpegurl; charset=utf-8",
  txt: "text/plain; charset=utf-8",
  md: "text/plain; charset=utf-8",
  html: "text/html; charset=utf-8",
  xml: "application/xml; charset=utf-8",
};

function mimeOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  return MIME[ext] || "application/octet-stream";
}

/** 去掉开头的斜杠和可选的 dist/ 前缀，得到 KV 里的 key */
function normKey(pathname) {
  let p = pathname.replace(/^\/+/, "");
  try {
    p = decodeURIComponent(p);
  } catch (_) {
    /* 保留原样 */
  }
  if (p.startsWith("dist/")) p = p.slice(5);
  return p;
}

/** 定长比较，避免 token 逐字符试探 */
function safeEq(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function text(msg, status = 200) {
  return new Response(msg + "\n", {
    status,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

async function handleUpload(req, env, key) {
  if (!key) return json({ ok: false, error: "缺少文件名" }, 400);

  const token = env.UPLOAD_TOKEN || "";
  if (!token) return json({ ok: false, error: "服务端没设 UPLOAD_TOKEN，请先在 Worker 设置里加一个 Secret" }, 500);
  if (!safeEq(req.headers.get("x-token") || "", token)) {
    return json({ ok: false, error: "token 不对" }, 401);
  }

  const body = await req.text();
  await env.TVKEEP.put(key, body);
  return json({ ok: true, key, bytes: body.length });
}

async function statusPage(env, url) {
  let meta = null;
  try {
    meta = await env.TVKEEP.get("_meta", "json");
  } catch (_) {
    /* 首次还没有 */
  }

  const origin = url.origin;
  const files = (meta && meta.files) || [];
  const rows = files
    .map((f) => `<tr><td><code>${f.path}</code></td><td>${(f.bytes / 1024).toFixed(1)} KB</td></tr>`)
    .join("");

  const updated = meta && meta.updated ? meta.updated : null;
  const ageH = updated ? (Date.now() - new Date(updated).getTime()) / 3600000 : null;
  const stale = ageH !== null && ageH > 48;

  const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tv-keep 状态</title>
<style>
  :root{color-scheme:light}
  body{margin:0;padding:32px 20px;background:#f6f7f9;color:#1a1a1a;
       font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:720px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:#6b7280;font-size:13px;margin-bottom:24px}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;margin-bottom:16px}
  .card h2{font-size:13px;font-weight:600;color:#6b7280;margin:0 0 12px;letter-spacing:.04em}
  .addr{display:block;background:#f3f4f6;border-radius:6px;padding:10px 12px;
        font-family:ui-monospace,Consolas,monospace;font-size:13px;word-break:break-all;
        color:#111827;text-decoration:none;margin-bottom:8px;border:1px solid #e5e7eb}
  .addr:hover{background:#eef2ff;border-color:#c7d2fe}
  .tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
       background:#dcfce7;color:#166534;font-weight:600}
  .tag.warn{background:#fef3c7;color:#92400e}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:5px 0;border-bottom:1px solid #f3f4f6;color:#374151}
  td:last-child{text-align:right;color:#6b7280;white-space:nowrap}
  code{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
  .hint{color:#6b7280;font-size:13px;margin:8px 0 0}
  details summary{cursor:pointer;color:#4f46e5;font-size:13px}
</style></head>
<body><div class="wrap">
  <h1>tv-keep</h1>
  <div class="sub">影视仓的固定地址 · 源在后面天天换，这个地址永远不变</div>

  <div class="card">
    <h2>数据状态</h2>
    <p style="margin:0 0 6px">
      ${
        updated
          ? `<span class="tag ${stale ? "warn" : ""}">${stale ? "超过 48 小时未更新" : "已更新"}</span>
             <span style="margin-left:8px;color:#6b7280;font-size:13px">${updated}</span>`
          : `<span class="tag warn">还没推过数据</span>`
      }
    </p>
    <p class="hint">${
      stale
        ? "在电脑上跑一次 run-local.bat 即可刷新，或检查计划任务是否还在跑。"
        : "每天由你家电脑跑完自动推上来；探测用的是你家宽带，不是云端机器。"
    }</p>
  </div>

  <div class="card">
    <h2>填进影视仓的地址</h2>
    <a class="addr" href="/dist/multi.json">${origin}/dist/multi.json</a>
    <p class="hint">多仓地址，推荐用这个——一个地址带全部线路，切源不用回设置页。</p>
    <details style="margin-top:12px">
      <summary>其他可用的地址</summary>
      <a class="addr" style="margin-top:10px" href="/dist/tvbox.json">${origin}/dist/tvbox.json</a>
      <p class="hint">单仓，只有一份站点列表，加载更快。</p>
      <a class="addr" href="/dist/live.m3u">${origin}/dist/live.m3u</a>
      <p class="hint">直播清单，M3U 格式，TiviMate 这类播放器直接用。</p>
      <a class="addr" href="/dist/live.txt">${origin}/dist/live.txt</a>
      <p class="hint">同一份直播清单的 TXT 格式，影视仓内置直播用这个。</p>
    </details>
  </div>

  <div class="card">
    <h2>已上线的文件（${files.length} 个）</h2>
    ${
      files.length
        ? `<table>${rows}</table>`
        : `<p class="hint">还没有。在本机跑 <code>cf\\push.py</code> 之后这里会列出来。</p>`
    }
  </div>

  <div class="card">
    <h2>体检报告</h2>
    <a class="addr" href="/dist/live-report.md">${origin}/dist/live-report.md</a>
    <a class="addr" href="/dist/vod-report.md">${origin}/dist/vod-report.md</a>
    <p class="hint">哪些台有几个源、哪些站被剔除、为什么剔除，都写在这里。</p>
  </div>
</div></body></html>`;

  return new Response(html, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const key = normKey(url.pathname);

    if (req.method === "PUT" || req.method === "POST") {
      return handleUpload(req, env, key);
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      return text("只支持 GET / PUT", 405);
    }

    if (key === "health") {
      const meta = await env.TVKEEP.get("_meta", "json").catch(() => null);
      return json({ ok: true, updated: meta ? meta.updated : null });
    }
    if (key === "" || key === "index.html") {
      return statusPage(env, url);
    }

    const buf = await env.TVKEEP.get(key, "arrayBuffer");
    if (!buf) {
      return text(
        `404 · KV 里没有 ${key}\n` +
          `先在电脑上跑一次 tv-keep\\run-local.bat（或 cf\\push.py）把产物推上来。`,
        404
      );
    }

    return new Response(buf, {
      headers: {
        "content-type": mimeOf(key),
        // 电视端会反复拉，给 3 分钟缓存：既省 KV 读次数，更新后也很快生效
        "cache-control": "public, max-age=180",
        "access-control-allow-origin": "*",
        "x-tvkeep-key": key,
      },
    });
  },
};
