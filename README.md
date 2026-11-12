# tv-keep · 影视仓点播/直播自动保活

电视上只填**一个永远不变的地址**，地址背后的源每天自动换。

- **直播**：从多个公开直播源里只挑你要的台 → 逐条探活、剔掉死链和广告占位 → 每台留 6 条备用 → 输出一条 m3u。
- **点播**：把你现在用的多仓地址聚合 → 递归展开所有线路（线路里还套线路也能展开）→ 逐站探活 → 死站剔掉 → 输出一份你自己的多仓配置。线路还是原来那些，但里面只剩活着的站。

零第三方依赖，只用 Python 标准库。托管两种都支持，都不花钱：**Cloudflare（推荐）** 或 GitHub Pages。

---

## 一、先说清楚一件事：探测分两档

这决定了整个工具靠不靠谱，值得读 30 秒。

云端机器的出口基本都在**境外**（GitHub Actions 是，Cloudflare Workers 也是）。而境外网络看国内直播源，结果和你在家完全不是一回事：大量运营商 CDN 的地址对境外 IP 返回 403，或者干脆不响应。**这不是源坏了，是测不到。**

如果每天在境外跑一次、把"测不到"当成"坏了"删掉，几天之后你的配置就被自己洗干净了。所以：

| 模式 | 怎么判断 | 用在哪 |
| --- | --- | --- |
| `geo_safe`（默认） | 只剔**确定已死**的：域名解析失败、404、连接被拒。**403 和超时一律算"疑点"保留**，排在确认可用之后 | 万一你在云端跑 |
| `strict` | 真拉分片、真测速率，按实测速度排序 | `run-local.bat`，用你家宽带跑 |
| `off` | 完全不联网验证，只做合并去重 | 网络特别差时兜底 |

`geo_safe` 模式下没有可信速度，排序改用两个跟地理位置无关的信号：**这条线连续活了几天**，以及**它是域名还是纯 IP**（域名走正规 CDN，纯 IP 多是个人中转，活得短）。

第一次跑所有线路都只活了 1 天，看起来排序差不多；跑上一周差距就出来了，稳的会浮到前面。你在家跑 `strict` 时测到的速度会写进 `dist/live-hist.json`，下次排序就参考它——所以**跑得越勤，排得越准**。

---

## 二、先选一条路

两条都能做到"地址永不变"，区别只在**探测跑在哪台机器上**：

| | **Cloudflare（推荐）** | GitHub |
| --- | --- | --- |
| 固定地址 | Worker 域名 | Pages |
| 探测跑在哪 | **你家电脑**，每天定时一次 | GitHub 的机器（境外） |
| 测速准不准 | 准，走你家宽带，能 `strict` 真测速 | 只能 `geo_safe`，境外测国内直播必然有偏差 |
| 代价 | 那天电脑得开着 | 完全不用管电脑 |
| 国内能不能打开 | `*.workers.dev` 常被污染，**建议绑自己的域名** | `*.github.io` 时好时坏 |

选 Cloudflare 的理由很实在：**探测这一步本来就不该在境外机器上跑**。而且 Cloudflare 免费版的限制（一次调用最多 50 个子请求、10ms CPU）刚好逼你把探测留在本地——这反而是对的架构，不是妥协。

**关于域名的实话**：`*.workers.dev` 在国内被 DNS 污染的几率不低。你有域名就绑上去（Cloudflare 里加一条 CNAME，2 分钟），没有域名就先试——能不能打开，用手机 4G 打开一次就知道，不行再退回 GitHub Pages。电视端拉的是几十 KB 的配置，慢一点无所谓，打不开才是问题。

---

## 三、Cloudflare 部署（推荐，约 15 分钟）

全程网页点击，**不用装 Node、不用碰命令行**。

### 1. 建 KV

控制台左侧 **Storage & Databases → KV → Create a namespace**，名字填 `tvkeep`。

### 2. 建 Worker

左侧 **Compute (Workers) → Workers & Pages → Create → Workers → Start with Hello World**，名字填 `tv-keep` → Deploy。

进去点 **Edit code**，把 `cf/worker.js` 的内容**整份**粘进去覆盖原有代码 → **Deploy**。

### 3. 绑 KV、设 token

回到这个 Worker 的 **Settings**：

- **Bindings → Add → KV Namespace**：Variable name 必须填 **`TVKEEP`**（大小写敏感，改一个字就报错），右边选刚建的 `tvkeep`。
- **Variables and Secrets → Add**：类型选 **Secret**，名字填 **`UPLOAD_TOKEN`**，值自己编一串长的（比如 `tvkeep-9f3a2c7e`）。**记下来**，下一步要用。

两处加完再 **Deploy** 一次。

### 4. 本机配好，推第一次

打开 `cf/config.json`，填你的 Worker 地址（**不要**带结尾斜杠、不要带 `/dist`）：

```json
{ "worker_url": "https://tv-keep.你的子域.workers.dev" }
```

在同一目录新建 `cf/.token`（文件名前面有个点），里面只写刚才那串 `UPLOAD_TOKEN`，保存。

然后双击 `run-local.bat`。三步跑完会自动推送，最后一行告诉你推了多少个文件。

> **推送有两条通道**：先直连 worker 域名（快）；如果这个域名在你的网络里打不开（`*.workers.dev` 被污染就会这样），脚本会自动改走 Cloudflare API，效果完全一样。想手动指定：`python cf\push.py --via wrangler`。

> `cf/.token` 已经在 `.gitignore` 里。它等于你配置的写权限，别发群里、别截图。

### 5. 看一眼状态页

浏览器打开 `https://你的Worker地址/`，它会直接告诉你：电视该填哪个地址、数据是什么时候更新的、上线了哪些文件。**如果显示"还没推过数据"，说明第 4 步没成功。**

### 6. 每天自动跑

Win+R 输入 `taskschd.msc` → **创建基本任务**：

- 触发器：每天，时间选你**通常开着电脑**的时段（比如 08:30）
- 操作：启动程序 → 填 `run-local.bat` 的完整路径
- 顺手在「条件」里取消勾选"只有在计算机使用交流电源时才启动"（笔记本才需要管这条）

电脑那天没开就不更新，电视照旧播上一次的结果，不影响使用。

### 7. 想改 Worker 代码？（可选，用命令行）

上面 1–5 步也能全部用命令做完，改完 `cf/worker.js` 重新发布就靠这个：

```bash
cd cf
npm i wrangler            # 只需一次
npx wrangler login        # 浏览器里点 Allow
npx wrangler deploy       # 改完代码跑这个
```

- 配置在 `cf/wrangler.toml`，KV 的 id 已经填好，不用动。
- 设密钥：`npx wrangler secret put UPLOAD_TOKEN`（粘一行回车）。
- 命令行的所有 KV 操作都要带 `--remote`，不加会写进本地模拟环境——这个坑很容易踩。

---

## 四、GitHub 部署（备选，约 10 分钟）

### 1. 建仓库

打开 https://github.com/new ，名字填 `tv-keep`，**不要**勾选任何初始化文件，创建。

### 2. 把文件传上去

仓库首页 → **Add file → Upload files** → 把本目录所有内容（`live/`、`vod/`、`.github/`、`net.py`、`run-local.bat`、`README.md`）拖进去。

> `.github` 是隐藏文件夹，网页上传容易漏。漏了就没有定时任务，但本地跑完全不受影响。

有 Git 基础更稳：

```bash
git init && git add -A && git commit -m init
git remote add origin https://github.com/<你的用户名>/tv-keep.git
git branch -M main && git push -u origin main
```

### 3. 开 Actions（定时任务）

仓库 → **Actions** 标签 → 如果提示要启用，点 **I understand my workflows, go ahead and enable them**。

想立刻验证一次：Actions 左侧 **tv-keep** → 右侧 **Run workflow** → 绿色按钮。跑 3–5 分钟，看日志里有没有 `[结果] 可用频道 x/35`。

### 4. 开 Pages（固定地址）

仓库 → **Settings → Pages → Source** 选 **Deploy from a branch** → Branch 选 `main`、目录 **/ (root)** → Save。

---

## 五、拿到固定地址，填进影视仓

**Cloudflare 用户**：`<你的地址>` 就是 `https://tv-keep.xxx.workers.dev`（或你绑的域名）

| 用途 | 地址 |
| --- | --- |
| **点播（用这个）** | `<你的地址>/dist/multi.json` |
| 点播（单仓，站点更少但更纯净） | `<你的地址>/dist/tvbox.json` |
| 直播 | `<你的地址>/dist/live.m3u` |
| 直播（TXT 格式） | `<你的地址>/dist/live.txt` |

**GitHub 用户**：把 `<用户名>` 换成你自己的

| 用途 | 地址 |
| --- | --- |
| **点播（用这个）** | `https://<用户名>.github.io/tv-keep/dist/multi.json` |
| 点播（单仓） | `https://<用户名>.github.io/tv-keep/dist/tvbox.json` |
| 直播 | `https://<用户名>.github.io/tv-keep/dist/live.m3u` |

**影视仓里的动作**

1. 设置 → 配置地址 → 粘贴 `multi.json` 那条 → 保存。
2. 回首页进「线路选择」，每条线路对应一个上游，里面只剩探活通过的站。
3. 直播不用单独填：点播配置的 `lives` 里已经内嵌了你自己那条 `live.m3u`。

> **GitHub 用户**：`github.io` 在个别网络下打不开，用备用地址（内容完全一样）：
> - `https://raw.githubusercontent.com/<用户名>/tv-keep/main/dist/multi.json`
> - `https://cdn.jsdelivr.net/gh/<用户名>/tv-keep@main/dist/multi.json`
>
> Cloudflare 用户如果 workers.dev 打不开，去 Worker → Settings → **Custom Domains** 绑自己的域名，地址变成 `https://tv.你的域名/dist/multi.json`，其余不变。

**这个地址永远不用再改。**

---

## 六、日常

绝大多数时候你什么都不用做。

**想看效果**：浏览器打开 `<你的地址>/dist/live-report.md`、`<你的地址>/dist/vod-report.md`（状态页上也有链接），或者直接看本机 `dist/` 里的同两个文件。关键三行：

- 直播：**今天哪几个台没找到可用线路** —— 这几个台的源该补了；
- 点播：**每条线路今天掉了多少站**；
- 点播：**本次剔除的站点** —— 谁挂了、为什么挂。

**想让结果更好**：手动双击一次 `run-local.bat` 就行。它用你家宽带做 `strict` 真测速，跑完自动推上线。"速度记进历史"就是靠这个累积的，跑得越多，排序越贴合你家网络。

**什么都没更新？** 先看状态页上的时间戳。超过 48 小时说明计划任务没跑成，通常是电脑那几天没开机，或者 Python 被卸载了。

---

## 七、按你的习惯调

### 直播：留下哪些台 → `live/channels.txt`

```
频道名 | 别名1,别名2 | 分组
CCTV-5 体育 | CCTV5,CCTV-5 | 央视频道
```

**不看的整行删掉。** 台越少，每台分到的探测预算越足。20 个台最舒服。

### 直播：加源 → `live/sources.txt`

一行一个 `备注 | 地址`。默认 5 个源是按实测覆盖率排的（饭太硬 33/35、iptv-org 24/35、vbskycn 20/35、zhoujie 12/35）。

用的是 jsDelivr 镜像而不是 `raw.githubusercontent.com`——国内对 raw 的解析经常失败。镜像挂了脚本会自动换回 raw 重试。

`ipv6.m3u` 那行默认注释掉了：台更全（31/35）、画质更好，但要求你的宽带和电视都支持 IPv6。能播再打开。

### 点播：把你正在用的地址粘进来 → `vod/sources.txt`

一行一个 `备注 | 地址`。**这一步最值得做**：你手机上收藏的那堆多仓地址，粘进来之后就变成后台输入，以后只管填自己这一个。

### 参数

`live/config.json`

| 参数 | 含义 |
| --- | --- |
| `probe_mode` | `geo_safe` / `strict` / `off`，见第一节 |
| `keep_per_channel` | 每个台保留几条备用线（默认 6，可以调小到 3–4） |
| `min_kbps` | 仅 `strict` 生效：低于这个码率不采用（默认 300） |
| `workers` | 并发数，网络好可加到 32 |
| `epg` | 填了就在 m3u 里带节目单，如 `https://live.fanmingming.cn/e.xml` |

`vod/config.json`

| 参数 | 含义 |
| --- | --- |
| `probe_mode` | 同上，默认 `geo_safe` |
| `max_per_upstream` | 每条线路最多保留几个站（默认 45） |
| `max_repos` | 最多展开几条线路（默认 40） |
| `max_probe_sites` | 单次最多探多少个独立 API（默认 600） |
| `keep_sites` | 单仓最多保留多少站（默认 90） |
| `min_keep_sites` | 安全阀：存活站少于这个数就不覆盖旧结果 |

---

## 八、三道保护（防止电视变黑屏）

1. **跌幅保护**：这次可用数量比上次少 40% 以上，直接放弃本次，保留旧结果。典型场景就是网络环境变了。
2. **空结果保护**：一条可用线路都没有时，绝不覆盖已有文件。
3. **疑点保留**：`geo_safe` 下 403/超时不会被当成死亡，所以"境外误杀"这条路被堵住了。

三道保护都只比较两次都存在的频道/站点，所以你删台、删源不会误触发。

真被拦下来时退出码是 2，日志里会写明原因。确认无误想强制覆盖，删掉 `dist/live-status.json` 或 `dist/vod-status.json` 再跑。

---

## 九、几句实话

1. **它不含任何影视内容**，只是配置整理和测速工具，源全由你自己填。看什么、是否合规，是你自己的责任。
2. **免费源一定会死**，这是结构性的。这个工具能做到「死了自动换」，做不到「永远不死」。
3. **jar 爬虫站（`csp_` 开头）无法在电脑上验证**，只能原样保留。这正是每条线路都必须保留上游自己 `spider` / `vars` 字段的原因——丢了它们，这类站全废。这是普通「合并去重」工具最容易踩的坑。
4. **`type:3` 的 js 源**（`api` 指向 `.js`）不是 JSON 接口，探测时看的是"脚本能不能下载下来"，不是"接口有没有返回列表"。混为一谈会把这些站全判死。
5. **上游塞了什么广告、什么跳转，本工具管不了**。挑口碑好的上游，别乱加来源不明的配置。
