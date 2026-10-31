"""历史快照访问验证：仅检查可达性，不做 HTML 解析。"""

import logging

import requests

logger = logging.getLogger(__name__)


def verify_snapshot(session: requests.Session, timestamp: str, original_url: str,
                    timeout: float) -> dict:
    """访问 Wayback 快照，记录 HTTP 状态码、最终 URL 与响应内容类型。

    使用 stream=True 只取响应头即关闭，不下载正文。
    返回 dict: archive_url / status_code / final_url / content_type / ok / error
    """
    archive_url = f"https://web.archive.org/web/{timestamp}/{original_url}"
    record = {
        "timestamp": timestamp,
        "original_url": original_url,
        "archive_url": archive_url,
        "status_code": None,
        "final_url": None,
        "content_type": None,
        "ok": False,
        "error": None,
    }
    try:
        resp = session.get(archive_url, timeout=timeout, allow_redirects=True, stream=True)
        record["status_code"] = resp.status_code
        record["final_url"] = str(resp.url)
        record["content_type"] = resp.headers.get("Content-Type", "")
        resp.close()
        # Wayback 重定向到最近快照后返回 2xx 即认为可访问
        record["ok"] = 200 <= resp.status_code < 300
    except requests.RequestException as exc:
        record["error"] = str(exc)
        logger.warning("快照访问失败 %s: %s", archive_url, exc)
    return record


def verify_all(session: requests.Session, tweet_index: dict, timeout: float,
               pause: float, max_fetch: int = 0) -> list[dict]:
    """按推文逐一验证快照，返回验证记录列表。

    每条推文只验证其最早的一个快照（代表该推文可恢复性），控制请求量。
    """
    results = []
    tweet_ids = sorted(tweet_index.keys())
    if max_fetch > 0:
        tweet_ids = tweet_ids[:max_fetch]

    total = len(tweet_ids)
    for i, tweet_id in enumerate(tweet_ids, 1):
        first = tweet_index[tweet_id]["snapshots"][0]
        record = verify_snapshot(session, first["timestamp"], first["original_url"], timeout)
        record["tweet_id"] = tweet_id
        results.append(record)
        if i % 10 == 0 or i == total:
            ok = sum(1 for r in results if r["ok"])
            logger.info("快照验证进度 %d/%d（成功 %d）", i, total, ok)
        if pause > 0:
            from time import sleep
            sleep(pause)
    return results
