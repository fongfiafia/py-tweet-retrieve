"""数据导出：JSON / CSV 保存与统计报告。"""

import csv
import json
import logging
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

CSV_FIELDS = [
    "tweet_id",
    "original_url",
    "archive_timestamp",
    "archive_url",
    "status_code",
    "mimetype",
    "digest",
]


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存 %s", path)


def save_csv(path: Path, tweet_index: dict) -> int:
    """每行 = 一条推文的一个历史快照。返回行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for tweet in sorted(tweet_index.values(), key=lambda t: t["tweet_id"]):
            for snap in tweet["snapshots"]:
                writer.writerow({
                    "tweet_id": tweet["tweet_id"],
                    "original_url": snap["original_url"],
                    "archive_timestamp": snap["timestamp"],
                    "archive_url": snap["archive_url"],
                    "status_code": snap["status_code"],
                    "mimetype": snap["mimetype"],
                    "digest": snap["digest"],
                })
                count += 1
    logger.info("已保存 %s（%d 行）", path, count)
    return count


def build_report(stats: dict) -> str:
    """生成统计报告文本。"""
    lines = []
    lines.append("=" * 60)
    lines.append("统计报告")
    lines.append("=" * 60)
    lines.append(f"1. 查询的 URL 模式数量        : {stats['pattern_count']}")
    lines.append("2. 各 URL 模式返回的 CDX 记录数:")
    for q in stats["pattern_queries"]:
        mark = " (无结果)" if q["row_count"] == 0 and not q["error"] else ""
        err = f" [错误: {q['error']}]" if q["error"] else ""
        lines.append(f"   - {q['pattern']}: {q['row_count']} 条{mark}{err}")
    lines.append(f"3. 找到的唯一推文数量          : {stats['unique_tweets']}")
    lines.append(f"4. 推文历史快照总数量          : {stats['total_snapshots']}")
    lines.append(f"5. 成功访问的快照数量          : {stats['verified_ok']}")
    lines.append(f"6. 访问失败的快照数量          : {stats['verified_fail']}")
    lines.append(f"7. 最早存档时间                : {stats['earliest'] or '-'}")
    lines.append(f"   最晚存档时间                : {stats['latest'] or '-'}")
    lines.append("8. 按年份统计推文存档数量:")
    for year in sorted(stats["snapshots_by_year"]):
        lines.append(f"   - {year}: {stats['snapshots_by_year'][year]}")
    lines.append("=" * 60)
    return "\n".join(lines)
