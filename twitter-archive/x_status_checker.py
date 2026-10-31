"""推文当前状态检测：通过 X Syndication 接口检查推文是否仍然存在。

状态分类（不确定一律 unknown，不猜测）：
- active      : 成功获取推文数据，且 Tweet ID 匹配
- deleted     : 返回明确的作者删除提示（Tombstone 等明确证据）
- unavailable : 存在明确的访问限制或其他不可用原因，但无法确认删除
- unknown     : 请求失败 / 429 / 超时 / 响应异常 / 无法判断

注意：HTTP 404/403/429 都不等于已删除；HTTP 200 也不等于推文存在。
"""

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from cdx_client import build_session
from media_extractor import extract_author, extract_media, extract_text

logger = logging.getLogger(__name__)

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

TOMBSTONE_DELETED_KEYWORDS = ("deleted", "removed", "no longer", "删除", "已删除")


def compute_syndication_token(tweet_id: str) -> str:
    """按 react-tweet 同款算法动态计算 token。

    JS: ((Number(id) / 1e6) ^ Math.floor(Number(id) % 2075653)).toString(16)
    不使用固定的 token=0。
    """
    def to_int32(value: float) -> int:
        value = int(value) & 0xFFFFFFFF
        return value - 0x100000000 if value >= 0x80000000 else value

    n = float(tweet_id)
    value = to_int32(n / 1e6) ^ to_int32(math.floor(n % 2075653))
    return format(value, "x")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_payload(tweet_id: str, payload) -> tuple[str, str | None, dict]:
    """根据 Syndication 返回的 JSON 分类状态。

    返回 (status, evidence, extra)；extra 含 text/author/media。
    """
    extra: dict = {}

    if not isinstance(payload, dict):
        return "unknown", "响应不是 JSON 对象", extra

    # Tombstone：删除/受限提示，需要进一步检查内容
    tombstone = payload.get("tombstone")
    if tombstone:
        texts = []

        def _collect(node):
            if isinstance(node, dict):
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)
            elif isinstance(node, str):
                texts.append(node)

        _collect(tombstone)
        joined = " ".join(texts).lower()
        if any(k in joined for k in TOMBSTONE_DELETED_KEYWORDS):
            return "deleted", f"Tombstone 提示删除: {' '.join(texts)[:200]}", extra
        return "unavailable", f"Tombstone 提示不可用: {' '.join(texts)[:200]}", extra

    errors = payload.get("errors")
    if errors:
        return "unknown", f"接口返回 errors: {json.dumps(errors, ensure_ascii=False)[:200]}", extra

    # ID 匹配才算 active
    returned_id = str(payload.get("id_str") or payload.get("id") or "")
    if returned_id == str(tweet_id):
        extra = {
            "text": extract_text(payload),
            "author": extract_author(payload),
            "media": extract_media(payload),
        }
        return "active", None, extra

    return "unknown", f"返回数据与请求 ID 不匹配 (got {returned_id!r})", extra


def check_tweet(session: requests.Session, tweet_id: str,
                timeout: float = 15.0, retries: int = 2) -> dict:
    """检测单条推文的当前状态。单条失败不抛异常，返回 unknown 记录。"""
    record = {
        "tweet_id": str(tweet_id),
        "current_status": "unknown",
        "status_source": "x_syndication",
        "http_status": None,
        "checked_at": _utc_now_iso(),
        "evidence": None,
        "error": None,
        "text": None,
        "author": None,
        "media": [],
    }

    token = compute_syndication_token(tweet_id)
    url = f"{SYNDICATION_URL}?id={tweet_id}&lang=en&token={token}"
    headers = {
        "Referer": "https://platform.twitter.com/",
        "Accept": "application/json",
    }

    attempts = retries + 1
    backoff = 2.0
    resp = None
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            # 429 表示请求受限（非删除），指数退避后重试
            if resp.status_code != 429:
                break
            last_error = f"HTTP 429 请求受限（第 {attempt} 次）"
            logger.warning("%s %s", tweet_id, last_error)
        except requests.RequestException as exc:
            last_error = f"请求异常: {exc}"
            logger.warning("%s 请求异常: %s", tweet_id, exc)
        if attempt < attempts:
            time.sleep(backoff)
            backoff *= 2

    if resp is None:
        record["error"] = last_error
        record["evidence"] = "无法获得有效响应"
        return record

    record["http_status"] = resp.status_code

    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError:
            record["evidence"] = f"HTTP 200 但响应非 JSON: {resp.text[:200]!r}"
            return record
        status, evidence, extra = _classify_payload(tweet_id, payload)
        record["current_status"] = status
        record["evidence"] = evidence
        record.update(extra)
        return record

    if resp.status_code == 404:
        # 404 ≠ 已删除：可能是接口不稳定、权限差异等
        record["evidence"] = f"HTTP 404（无法区分删除/不存在/接口不稳定）: {resp.text[:200]!r}"
        return record
    if resp.status_code == 403:
        record["current_status"] = "unavailable"
        record["evidence"] = f"HTTP 403 访问受限: {resp.text[:200]!r}"
        return record
    if resp.status_code == 429:
        record["evidence"] = f"HTTP 429 请求受限（重试 {attempts} 次后仍受限），不判定删除"
        return record

    record["evidence"] = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
    return record


def run_inspection(session: requests.Session, tweets: list[dict],
                   pause: float = 1.5, timeout: float = 15.0,
                   retries: int = 2, on_progress=None) -> dict:
    """逐条检测 tweets 列表（来自 tweets.json），返回结果 payload。

    on_progress(event) 用于向前端推送进度，event 为 dict。
    """
    records: list[dict] = []
    total = len(tweets)
    if on_progress:
        on_progress({"type": "start", "total": total})

    for index, tweet in enumerate(tweets, 1):
        tweet_id = str(tweet["tweet_id"])
        if on_progress:
            on_progress({"type": "tweet_start", "tweet_id": tweet_id,
                         "index": index, "total": total})
        record = check_tweet(session, tweet_id, timeout=timeout, retries=retries)
        records.append(record)
        if on_progress:
            on_progress({"type": "tweet_done", "tweet_id": tweet_id,
                         "index": index, "total": total,
                         "status": record["current_status"],
                         "ok": record["current_status"] in ("active", "deleted", "unavailable")})
        if index < total and pause > 0:
            time.sleep(pause)

    statistics = {"active": 0, "deleted": 0, "unavailable": 0, "unknown": 0}
    for rec in records:
        statistics[rec["current_status"]] += 1

    if on_progress:
        on_progress({"type": "done"})
    return {
        "generated_at": _utc_now_iso(),
        "total_tweets": total,
        "statistics": statistics,
        "tweets": records,
    }


def save_results(path: Path, payload: dict) -> None:
    """原子保存，避免任务中断导致 JSON 文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    logger.info("已保存 %s", path)


def load_tweets(output_dir: Path) -> list[dict]:
    """读取现有 tweets.json，不做任何网络请求。"""
    path = output_dir / "tweets.json"
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("tweets") or []


def main() -> int:
    parser = argparse.ArgumentParser(description="检测 tweets.json 中推文的当前状态")
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--pause", type=float, default=1.5, help="请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--proxy", default=None, help="显式 HTTP 代理")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    tweets = load_tweets(args.output)
    logger.info("从 %s 读取到 %d 条推文", args.output / "tweets.json", len(tweets))

    session = build_session(proxy=args.proxy)
    payload = run_inspection(session, tweets, pause=args.pause,
                             timeout=args.timeout, retries=args.retries,
                             on_progress=lambda ev: logger.info("%s", ev))
    save_results(args.output / "x_status_results.json", payload)

    stats = payload["statistics"]
    print(f"检测完成: {payload['total_tweets']} 条 | "
          f"active={stats['active']} deleted={stats['deleted']} "
          f"unavailable={stats['unavailable']} unknown={stats['unknown']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
