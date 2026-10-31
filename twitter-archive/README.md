# twitter-archive

Twitter/X 历史推文存档查询工具。通过 Internet Archive 的 Wayback Machine CDX API
查询指定用户的历史推文存档索引，识别推文 URL，验证快照可访问性，导出 JSON/CSV。

不使用 Selenium/Playwright，不调用 X 官方 API，不做登录绕过。

## 用法

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 默认查询 Cyber_Sudo
.venv/bin/python main.py

# 自定义参数
.venv/bin/python main.py --user SomeUser --output output --pause 1.5 --timeout 60
.venv/bin/python main.py --max-fetch 50        # 只验证前 50 条推文的快照
.venv/bin/python main.py --skip-verify         # 只做 CDX 查询与推文识别
.venv/bin/python main.py -v                    # DEBUG 日志
```

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
├── main.py            # 入口、命令行参数、整体流程
├── cdx_client.py      # CDX API 请求、重试、resumeKey 分页
├── archive_fetcher.py # 历史快照访问验证（仅可达性，不解析 HTML）
├── tweet_parser.py    # 推文 URL 识别、推文 ID 去重
├── exporter.py        # JSON/CSV 导出、统计报告
├── requirements.txt
└── output/
    ├── cdx_results.json     # 各 URL 模式的 CDX 原始结果与查询日志
    ├── tweets.json          # 去重后的推文列表（含每个推文的全部快照）
    ├── tweets.csv           # 每行 = 一条推文的一个历史快照
    └── verify_results.json  # 快照访问验证结果
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
