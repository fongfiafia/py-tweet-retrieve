#!/usr/bin/env python3
"""Twitter/X 历史推文存档查询工具。

通过 Wayback Machine CDX API 查询指定用户的历史推文存档索引，
识别推文 URL，验证快照可访问性，并导出 JSON/CSV 与统计报告。
"""

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

from archive_fetcher import verify_all
from cdx_client import build_session, query_cdx
from exporter import build_report, save_csv, save_json
from tweet_parser import build_tweet_index

logger = logging.getLogger("twitter-archive")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="查询指定 Twitter/X 用户在 Wayback Machine 的历史推文存档",
    )
    parser.add_argument("--user", default="Cyber_Sudo", help="目标用户名（默认 Cyber_Sudo）")
    parser.add_argument("--output", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 超时秒数")
    parser.add_argument("--pause", type=float, default=1.0, help="请求间隔秒数")
    parser.add_argument("--retries", type=int, default=4, help="瞬态错误最大重试次数")
    parser.add_argument("--proxy", default=None,
                        help="显式 HTTP 代理（如 http://127.0.0.1:7897），设置后忽略环境代理变量")
    parser.add_argument("--max-fetch", type=int, default=0,
                        help="最多验证多少条推文的快照（0 = 全部）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过快照访问验证")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_url_patterns(username: str) -> list[str]:
    """不同历史时期的域名与 URL 格式都要覆盖。"""
    return [
        f"twitter.com/{username}",
        f"twitter.com/{username}/*",
        f"x.com/{username}",
        f"x.com/{username}/*",
    ]


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    username = args.user.lstrip("@")
    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    session = build_session(max_retries=args.retries, proxy=args.proxy)
    patterns = build_url_patterns(username)
    logger.info("目标用户: %s，共 %d 个 URL 模式", username, len(patterns))

    # ---- 第一步：CDX 查询 ----
    pattern_queries = []
    all_cdx_rows = []
    for i, pattern in enumerate(patterns, 1):
        logger.info("[%d/%d] 查询 CDX: %s", i, len(patterns), pattern)
        result = query_cdx(session, pattern, timeout=args.timeout, pause=args.pause)
        pattern_queries.append(result)
        all_cdx_rows.extend(result["rows"])
        if result["error"]:
            logger.error("模式 %s 查询出错: %s", pattern, result["error"])
        time.sleep(args.pause)

    # 保存 CDX 原始结果（含每个模式的查询日志与错误信息）
    save_json(output_dir / "cdx_results.json",
              {"username": username, "queries": pattern_queries})

    if not all_cdx_rows:
        logger.error("所有模式均未取到 CDX 记录，无法继续。")
        return 1

    # ---- 第二步：识别推文 ----
    tweet_index = build_tweet_index(all_cdx_rows, username)
    if not tweet_index:
        logger.error("未识别出任何推文 URL。")
        return 1

    # ---- 第四步：先落盘推文列表（验证失败也不丢数据）----
    tweets_for_json = sorted(tweet_index.values(), key=lambda t: t["first_timestamp"])
    save_json(output_dir / "tweets.json",
              {"username": username, "tweet_count": len(tweets_for_json), "tweets": tweets_for_json})
    csv_rows = save_csv(output_dir / "tweets.csv", tweet_index)

    # ---- 第三步：快照可访问性验证 ----
    verified_ok = verified_fail = 0
    verify_records = []
    if args.skip_verify:
        logger.info("已跳过快照验证。")
    else:
        verify_records = verify_all(
            session, tweet_index,
            timeout=args.timeout, pause=args.pause, max_fetch=args.max_fetch,
        )
        verified_ok = sum(1 for r in verify_records if r["ok"])
        verified_fail = len(verify_records) - verified_ok
        save_json(output_dir / "verify_results.json",
                  {"username": username, "records": verify_records})

    # ---- 第五步：统计报告 ----
    timestamps = [snap["timestamp"] for t in tweet_index.values() for snap in t["snapshots"]]
    year_counter = Counter(ts[:4] for ts in timestamps)
    stats = {
        "pattern_count": len(patterns),
        "pattern_queries": pattern_queries,
        "unique_tweets": len(tweet_index),
        "total_snapshots": csv_rows,
        "verified_ok": verified_ok,
        "verified_fail": verified_fail,
        "earliest": min(timestamps) if timestamps else None,
        "latest": max(timestamps) if timestamps else None,
        "snapshots_by_year": dict(year_counter),
    }
    print(build_report(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
