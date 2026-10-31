"""推文 URL 识别与去重：从 CDX 记录中筛选真正的推文页面。"""

import logging
import re
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# /{username}/status/{id} 后可跟子路径（photo/1、video/1 等，仍属同一条推文页面）
STATUS_RE = re.compile(r"^/(?P<user>[A-Za-z0-9_]{1,15})/status/(?P<tweet_id>\d{5,25})(?P<rest>/.*)?$")

# 这些子路径视为同一条推文的页面变体，归一化到基础推文 URL
TWEET_PAGE_SUFFIX_RE = re.compile(r"^/(photo|video)(/\d+)?$", re.IGNORECASE)

# 这些子路径不是推文页面本身，直接排除
EXCLUDED_SUFFIX_RE = re.compile(r"^/(analytics|quotes|retweets|likes|retweets/with_comments)$", re.IGNORECASE)


def normalize_original(original: str) -> str:
    """把 CDX 里的 original 字段规整为带 scheme 的完整 URL。

    CDX 返回可能是 'twitter.com/xx' 或 'https://twitter.com/xx'，统一补 scheme。
    """
    url = original.strip()
    if "://" not in url:
        url = "https://" + url
    return url


def extract_tweet_record(cdx_row: dict, username: str) -> dict | None:
    """判断一条 CDX 记录是否是推文页面。

    命中返回 {"tweet_id", "original_url"}；否则返回 None。
    - 匹配 /{username}/status/{id}（用户名不区分大小写）
    - /status/{id}/photo/1、/video/1 归一化为同一条推文
    - /analytics 等附加页排除
    """
    url = normalize_original(cdx_row["original"])
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https"):
        return None

    m = STATUS_RE.match(parts.path)
    if not m:
        return None
    if m.group("user").lower() != username.lower():
        return None

    rest = m.group("rest") or ""
    if rest:
        if TWEET_PAGE_SUFFIX_RE.match(rest):
            # 归一化为基础推文 URL
            url = f"{parts.scheme}://{parts.netloc}/{m.group('user')}/status/{m.group('tweet_id')}"
        elif EXCLUDED_SUFFIX_RE.match(rest):
            return None
        else:
            # 未知子路径，保守排除
            logger.debug("排除未知子路径: %s", url)
            return None

    return {"tweet_id": m.group("tweet_id"), "original_url": url}


def build_tweet_index(cdx_rows: list[dict], username: str) -> dict:
    """把 CDX 行构建为按 tweet_id 去重的索引。

    返回 {tweet_id: {"tweet_id", "original_url", "snapshots": [...]}}，
    snapshot 含 timestamp/original_url/statuscode/mimetype/digest/archive_url。
    同一推文保留多个不同历史存档时间（digest 已在 CDX 端 collapse 去重）。
    """
    index: dict[str, dict] = {}
    skipped = 0
    for row in cdx_rows:
        rec = extract_tweet_record(row, username)
        if rec is None:
            skipped += 1
            continue
        tweet_id = rec["tweet_id"]
        entry = index.setdefault(
            tweet_id,
            {"tweet_id": tweet_id, "original_url": rec["original_url"], "snapshots": []},
        )
        entry["snapshots"].append(
            {
                "timestamp": row["timestamp"],
                "original_url": rec["original_url"],
                "status_code": row["statuscode"],
                "mimetype": row["mimetype"],
                "digest": row["digest"],
                "archive_url": f"https://web.archive.org/web/{row['timestamp']}/{rec['original_url']}",
            }
        )

    for entry in index.values():
        entry["snapshots"].sort(key=lambda s: s["timestamp"])
        entry["first_timestamp"] = entry["snapshots"][0]["timestamp"]
        entry["last_timestamp"] = entry["snapshots"][-1]["timestamp"]

    logger.info("CDX 记录 %d 条 → 唯一推文 %d 条（非推文记录 %d 条被排除）",
                len(cdx_rows), len(index), skipped)
    return index
