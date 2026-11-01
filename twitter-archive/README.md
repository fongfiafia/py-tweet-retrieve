# twitter-archive

Twitter/X 历史推文存档查询工具。通过 Internet Archive 的 Wayback Machine CDX API
查询指定用户的历史推文存档索引，识别推文 URL，验证快照可访问性，导出 JSON/CSV。

不使用 Selenium/Playwright，不调用 X 官方 API，不做登录绕过。

## 运行方式

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 历史存档查询（第一阶段）
.venv/bin/python main.py --proxy http://127.0.0.1:7897

# 本地调试页面
.venv/bin/python local_server.py --proxy http://127.0.0.1:7897
# 打开 http://127.0.0.1:8000
```

## 推文状态检测与媒体展示（第一阶段）

- `x_status_checker.py` — 通过 X Syndication 接口（`cdn.syndication.twimg.com/tweet-result`，
  token 按 react-tweet 同款算法动态计算）检测推文当前状态：active / deleted / unavailable / unknown。
  404/403/429 一律不判定为删除，不确定时保留 unknown。可独立运行：
  `.venv/bin/python x_status_checker.py --proxy http://127.0.0.1:7897`
- `media_extractor.py` — 从 Syndication JSON 提取图片（?name=orig 原图）、视频（最高 bitrate MP4）、GIF（animated_gif）。
- `local_server.py` — 纯标准库本地服务：`/api/data` 读本地 JSON，`/api/inspect` 启动检测，
  `/api/inspect/progress` 轮询进度；检测结果原子写入 `output/x_status_results.json`。
- `static/` — 原生 HTML/CSS/JS 调试页面：统计区、状态/媒体筛选、推文卡片、
  图片网格（点击开原图）、视频页内播放、媒体加载失败兜底。
  Debug Preview 按钮只读本地 JSON，不产生任何外部请求。

## 已删除推文的历史媒体恢复（Deleted Tweet Media Recovery）

- `historical_media_recovery.py` — 针对 `current_status=deleted` 的推文：
  1. 遍历其全部 Wayback 历史快照（按时间排序，优先 `id_` 修饰符取原始归档响应，失败退回普通回放）；
  2. 从 HTML / 内嵌 JSON 提取媒体 URL（pbs.twimg.com/media、video.twimg.com、og:image 等，
     解码 HTML entity 与 JSON 转义，按 base URL 去重并保留来源快照；识别 `tweet_video` 路径为 animated_gif，缩略图不误判为视频）；
  3. 每个媒体 URL 先验证原始 X CDN（校验 Content-Type，不只看 200）；
  4. 不可访问则单独查询 Wayback CDX（带缓存、URL 编码、按 MIME 过滤、优先接近推文快照时间），
     用实际返回的 timestamp 构造 `{ts}id_/{original}` 并再次验证 Content-Type；
  5. 状态：recovered / url_found / not_archived / unknown（`recovered` 必须经实际媒体响应验证）；
  6. 同时从 og:title / og:description 恢复推文正文与作者（twitter.com 时期快照有效，
     x.com SPA 快照无 meta 数据时如实记录为空）。
  可独立运行：`.venv/bin/python historical_media_recovery.py --proxy http://127.0.0.1:7897`
- 结果写入 `output/media_recovery_results.json`（原子保存，不覆盖其他文件）。
- 页面新增 Recover Deleted Media 按钮、恢复统计区、deleted 推文卡片的恢复区域
  （恢复来源 Original CDN / Wayback、存档时间、打开媒体入口、失败原因）。
  Debug Preview 完全离线，会读取已存在的恢复结果。

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--user` | `Cyber_Sudo` | 目标用户名 |
| `--output` | `output` | 输出目录 |
| `--timeout` | `60` | 单次 HTTP 超时（秒） |
| `--pause` | `1.0` | 请求间隔（秒），减轻对 IA 的压力 |
| `--retries` | `4` | 429/500/502/503/504 及连接错误的最大重试次数 |
| `--max-fetch` | `0` | 最多验证多少条推文的快照（0 = 全部） |
| `--skip-verify` | 关 | 跳过快照访问验证 |
| `-v` | 关 | DEBUG 日志 |

## 项目结构

```text
twitter-archive/
├── main.py            # 入口、命令行参数、整体流程（Wayback 存档查询）
├── cdx_client.py      # CDX API 请求、重试、resumeKey 分页
├── archive_fetcher.py # 历史快照访问验证（仅可达性，不解析 HTML）
├── tweet_parser.py    # 推文 URL 识别、推文 ID 去重
├── exporter.py        # JSON/CSV 导出、统计报告
├── x_status_checker.py # 推文当前状态检测（X Syndication）
├── media_extractor.py  # Syndication 媒体资源提取
├── historical_media_recovery.py # 已删除推文的历史媒体恢复
├── local_server.py     # 本地调试页面服务（标准库）
├── static/             # 前端页面（原生 HTML/CSS/JS）
├── requirements.txt
└── output/
    ├── cdx_results.json     # 各 URL 模式的 CDX 原始结果与查询日志
    ├── tweets.json          # 去重后的推文列表（含每个推文的全部快照）
    ├── tweets.csv           # 每行 = 一条推文的一个历史快照
    ├── verify_results.json  # 快照访问验证结果
    ├── x_status_results.json # 推文当前状态与媒体检测结果
    └── media_recovery_results.json # 已删除推文的历史媒体恢复结果
```

## 实现要点

- **URL 模式覆盖**：分别查询 `twitter.com/{user}`、`twitter.com/{user}/*`、
  `x.com/{user}`、`x.com/{user}/*`，覆盖不同历史时期的域名与路径格式。
- **分页**：CDX 请求带 `showResumeKey=true`，用 `resumeKey` 翻页取全数据，
  避免结果被截断；每个模式的查询日志（含页数与错误）保留在 `cdx_results.json`。
- **推文识别**：只保留 `/{user}/status/{id}` 形式的 URL（用户名不区分大小写）；
  `/status/{id}/photo/1`、`/video/1` 归一化为同一条推文；`/analytics` 等附加页与
  其他子路径排除；按推文 ID 去重，但保留同一推文的多个不同历史快照时间。
- **快照验证**：只检查可达性（HTTP 状态码、最终 URL、Content-Type），
  `stream=True` 取响应头后立即关闭，不下载正文。
- **礼貌性**：请求间隔 + 指数退避重试 + `Retry-After` 遵循。
