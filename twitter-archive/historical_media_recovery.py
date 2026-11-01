"""已删除推文的历史媒体恢复（Deleted Tweet Media Recovery）。

流程：
1. 从 x_status_results.json 取 current_status=deleted 的推文。
2. 从 tweets.json 取这些推文的全部历史快照（按时间从早到晚）。
3. 逐快照抓取 HTML：优先 id_ 修饰符（原始归档响应），失败退回普通回放 URL。
4. 从 HTML / 内嵌 JSON 提取 X 媒体 URL（pbs.twimg.com/media、video.twimg.com、
   og:image、twitter:image 等），相同媒体 URL 去重但保留来源快照。
5. 每个媒体 URL 先验证原始 X CDN 是否真的可访问（校验 Content-Type，不只看 200）。
6. 原始 CDN 不可访问时，对该媒体 URL 单独查询 Wayback CDX（带缓存），
   用实际返回的 timestamp + original 构造 id_ 访问地址并验证 Content-Type。
7. 结果原子写入 output/media_recovery_results.json。

不重新搜索账号 CDX、不重新执行 Syndication、不经后端代理媒体内容。
"""

import argparse
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from cdx_client import build_session, query_cdx
from x_status_checker import load_tweets, save_results

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024  # 快照 HTML 最多读 5MB，够提取 URL

PBS_MEDIA_RE = re.compile(
    r"https?://pbs\.twimg\.com/media/[A-Za-z0-9_\-]+(?:\.[A-Za-z]{3,4})?(?:\?[^\s\"'<>\\)\]]*)?",
    re.IGNORECASE,
)
VIDEO_MEDIA_RE = re.compile(
    r"https?://video\.twimg\.com/[A-Za-z0-9_\-/.]+(?:\?[^\s\"'<>\\)\]]*)?",
    re.IGNORECASE,
)
META_IMAGE_RE = re.compile(
    r"(?:property|name)\s*=\s*['\"](?:og:image|twitter:image)['\"][^>]*content\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
PIC_LINK_RE = re.compile(r"https?://pic\.twitter\.com/[A-Za-z0-9]+", re.IGNORECASE)

# 旧格式 URL 变体后缀（...jpg:large）
LEGACY_SUFFIX_RE = re.compile(r":(orig|large|medium|small|thumb|bigger|900x900)$", re.IGNORECASE)

# 这些路径片段表示视频缩略图，不是视频文件本身
THUMB_MARKERS = ("thumb",)


META_TEXT_RES = {
    "og:description": re.compile(
        r"(?:property|name)\s*=\s*['\"]og:description['\"][^>]*content\s*=\s*['\"]([^'\"]*)['\"]",
        re.IGNORECASE),
    "og:title": re.compile(
        r"(?:property|name)\s*=\s*['\"]og:title['\"][^>]*content\s*=\s*['\"]([^'\"]*)['\"]",
        re.IGNORECASE),
    "twitter:description": re.compile(
        r"(?:property|name)\s*=\s*['\"]twitter:description['\"][^>]*content\s*=\s*['\"]([^'\"]*)['\"]",
        re.IGNORECASE),
    "twitter:title": re.compile(
        r"(?:property|name)\s*=\s*['\"]twitter:title['\"][^>]*content\s*=\s*['\"]([^'\"]*)['\"]",
        re.IGNORECASE),
}


def extract_text_meta(html_text: str) -> dict:
    """从存档 HTML 的 meta 标签恢复推文正文与作者。

    twitter.com 时期的快照在 og:description / og:title 里带推文原文，
    x.com（SPA）快照通常没有。返回 {"text", "author", "title"}，找不到为 None。
    """
    values = {}
    for name, rex in META_TEXT_RES.items():
        m = rex.search(html_text)
        if m:
            value = html.unescape(m.group(1)).strip()
            # og:description 常被弯引号包裹
            if len(value) >= 2 and value[0] in "“\"" and value[-1] in "”\"":
                value = value[1:-1].strip()
            if value:
                values[name] = value
    text = values.get("og:description") or values.get("twitter:description")
    title = values.get("og:title") or values.get("twitter:title")
    author = None
    if title:
        m = re.match(r"^(.*?)\s+on Twitter", title)
        if m:
            author = m.group(1)
    return {"text": text, "author": author, "title": title}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(raw: str) -> str:
    """解码 HTML entity 与 JSON 字符串转义，便于提取内嵌 JSON 里的 URL。"""
    text = raw.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u0026", "&").replace("\\u0026amp;", "&")
    text = html.unescape(text)
    return text


def _strip_tail(url: str) -> str:
    return url.rstrip("\\\"',;).]>")


def split_legacy_variant(url: str) -> tuple[str, str | None]:
    """拆掉旧格式变体后缀（...jpg:large），返回 (base, variant)。"""
    m = LEGACY_SUFFIX_RE.search(url)
    if m:
        return url[: m.start()], m.group(1).lower()
    return url, None


def media_key(url: str) -> str:
    """媒体去重 key：pbs.twimg.com/media 去掉扩展名与 query；video 去掉 query。"""
    base, _ = split_legacy_variant(_strip_tail(url))
    parts = urlsplit(base)
    path = parts.path
    if "pbs.twimg.com/media" in path:
        path = re.sub(r"\.[A-Za-z]{3,4}$", "", path)
    return f"{parts.scheme or 'https'}://{parts.netloc}{path}"


def _url_score(url: str) -> int:
    """变体质量打分：orig > 无参/large > medium > small。"""
    base, legacy = split_legacy_variant(url)
    lower = base.lower()
    if legacy == "orig" or "name=orig" in lower:
        return 4
    if legacy == "large":
        return 2
    if "name=large" in lower:
        return 2
    if legacy or "name=" in lower:
        return 1
    return 3  # 无参数的原始形式


def classify_and_collect(text: str) -> dict:
    """从规整化文本中提取并分类媒体 URL。"""
    found = {"photo": [], "video": [], "animated_gif": []}
    seen = set()

    def _add(mtype: str, url: str) -> None:
        url = _strip_tail(url)
        if not url or url in seen:
            return
        seen.add(url)
        found[mtype].append(url)

    for url in PBS_MEDIA_RE.findall(text):
        _add("photo", url)
    for url in VIDEO_MEDIA_RE.findall(text):
        path = urlsplit(url).path.lower()
        if any(marker in path for marker in THUMB_MARKERS):
            continue  # 缩略图不算视频本体
        mtype = "animated_gif" if "tweet_video" in path else "video"
        _add(mtype, url)
    for url in META_IMAGE_RE.findall(text):
        url = _strip_tail(url)
        # og:image 补充：可能不是 pbs 域名，也算 photo 候选
        if url.startswith("http") and url not in seen:
            seen.add(url)
            found["photo"].append(url)
    return found


def merge_snapshot_media(found_by_snapshot: list[dict]) -> dict:
    """跨快照合并：相同媒体 URL 去重，保留所有来源快照时间与最佳变体。

    返回 {key: {"type", "best_url", "all_urls", "source_snapshot_timestamps"}}
    """
    merged: dict[str, dict] = {}
    for item in found_by_snapshot:
        ts = item["timestamp"]
        for mtype, urls in item["found"].items():
            for url in urls:
                key = media_key(url)
                entry = merged.setdefault(key, {
                    "type": mtype,
                    "best_url": url,
                    "all_urls": set(),
                    "source_snapshot_timestamps": [],
                })
                entry["all_urls"].add(url)
                if ts not in entry["source_snapshot_timestamps"]:
                    entry["source_snapshot_timestamps"].append(ts)
                if _url_score(url) > _url_score(entry["best_url"]):
                    entry["best_url"] = url
    for entry in merged.values():
        entry["source_snapshot_timestamps"].sort()
        entry["all_urls"] = sorted(entry["all_urls"])
    return merged


def _build_photo_candidates(best_url: str) -> list[str]:
    """为图片构造验证候选：最佳变体优先，其次构造 name=orig 形式。"""
    candidates = [best_url]
    base, _ = split_legacy_variant(best_url)
    parts = urlsplit(base)
    path_no_query = parts.path
    ext_match = re.search(r"\.([A-Za-z]{3,4})$", path_no_query)
    ext = ext_match.group(1).lower() if ext_match else "jpg"
    clean = f"{parts.scheme}://{parts.netloc}{path_no_query}"
    for cand in (
        f"{clean}?format={ext}&name=orig",
        f"{clean}?name=orig",
    ):
        if cand not in candidates:
            candidates.append(cand)
    return candidates[:3]


def check_media_url(session: requests.Session, url: str, expected_prefix: str,
                    timeout: float) -> dict:
    """验证媒体 URL 是否真的返回媒体内容（看 Content-Type，不只看 200）。

    stream=True 只取响应头即关闭，不下载完整文件。
    """
    try:
        resp = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
        ct = (resp.headers.get("Content-Type") or "").lower()
        length = resp.headers.get("Content-Length")
        status = resp.status_code
        final_url = str(resp.url)
        resp.close()
        ok = status == 200 and ct.startswith(expected_prefix)
        # HTML 错误页即使 200 也不算媒体
        if ok and "text/html" in ct:
            ok = False
        return {"ok": ok, "status": status, "content_type": ct,
                "content_length": length, "final_url": final_url, "error": None}
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "content_type": None,
                "content_length": None, "final_url": None, "error": str(exc)}


def _cdx_media_candidates(records: list[dict], source_ts: str | None) -> list[dict]:
    """从 CDX 记录中筛选媒体类型的快照，按接近推文页面快照时间排序。"""
    valid = []
    for row in records:
        mime = (row.get("mimetype") or "").lower()
        if mime.startswith("image/") or mime.startswith("video/"):
            valid.append(row)
    if not valid:
        return []
    if source_ts:
        source_day = int(str(source_ts)[:8])
        valid.sort(key=lambda r: abs(int(str(r["timestamp"])[:8]) - source_day))
    else:
        valid.sort(key=lambda r: r["timestamp"])
    return valid


def recover_single_media(session: requests.Session, key: str, entry: dict,
                         timeout: float, pause: float, cdx_cache: dict) -> dict:
    """恢复单个媒体：先原始 CDN，后 Wayback CDX。"""
    record = {
        "type": entry["type"],
        "original_url": entry["best_url"],
        "all_found_urls": entry["all_urls"],
        "display_url": None,
        "recovery_status": "unknown",
        "recovery_source": "none",
        "width": None,
        "height": None,
        "content_type": None,
        "content_length": None,
        "archive_timestamp": None,
        "archive_url": None,
        "source_snapshot_timestamps": entry["source_snapshot_timestamps"],
        "evidence": None,
        "error": None,
    }
    expected_prefix = "image/" if entry["type"] == "photo" else "video/"

    # ---- 第五步：原始 X CDN ----
    candidates = (_build_photo_candidates(entry["best_url"])
                  if entry["type"] == "photo" else [entry["best_url"]])
    cdn_errors = []
    for cand in candidates:
        check = check_media_url(session, cand, expected_prefix, timeout)
        if check["ok"]:
            record.update({
                "recovery_status": "recovered",
                "recovery_source": "original_cdn",
                "display_url": cand,
                "content_type": check["content_type"],
                "content_length": check["content_length"],
            })
            return record
        cdn_errors.append(f"{cand} → HTTP {check['status']} {check['content_type'] or check['error']}")
        if pause > 0:
            time.sleep(pause)
    record["evidence"] = "原始 CDN 不可访问: " + " | ".join(cdn_errors)

    # ---- 第六步：Wayback CDX 单独查询（带缓存，同一 URL 不重复查）----
    lookup_urls = []
    for u in [entry["best_url"]] + entry["all_urls"]:
        base, _ = split_legacy_variant(u)
        parts = urlsplit(base)
        bare = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if bare not in lookup_urls:
            lookup_urls.append(bare)

    cdx_error = None
    cdx_candidates = []
    for lookup in lookup_urls[:2]:  # 最多查两种形式，控制请求量
        cache_key = ("cdx", lookup)
        if cache_key in cdx_cache:
            result = cdx_cache[cache_key]
        else:
            result = query_cdx(session, lookup, timeout=timeout, pause=pause)
            cdx_cache[cache_key] = result
            if pause > 0:
                time.sleep(pause)
        if result["error"]:
            cdx_error = result["error"]
            continue
        cdx_candidates = _cdx_media_candidates(
            result["rows"], entry["source_snapshot_timestamps"][0])
        if cdx_candidates:
            break

    if cdx_error and not cdx_candidates:
        record["recovery_status"] = "unknown"
        record["error"] = f"CDX 查询失败: {cdx_error}"
        return record
    if not cdx_candidates:
        record["recovery_status"] = "not_archived"
        record["recovery_source"] = "none"
        record["evidence"] = (record["evidence"] or "") + "；CDX 查询成功但无媒体存档"
        return record

    # ---- 验证 Wayback 媒体是否真的可访问 ----
    wb_errors = []
    for row in cdx_candidates[:3]:
        ts = row["timestamp"]
        original = row["original"]
        wb_url = f"https://web.archive.org/web/{ts}id_/{original}"
        check = check_media_url(session, wb_url, expected_prefix, timeout)
        if check["ok"]:
            record.update({
                "recovery_status": "recovered",
                "recovery_source": "wayback",
                "display_url": wb_url,
                "content_type": check["content_type"],
                "content_length": check["content_length"],
                "archive_timestamp": ts,
                "archive_url": wb_url,
            })
            return record
        wb_errors.append(f"{ts} → HTTP {check['status']} {check['content_type'] or check['error']}")
        if pause > 0:
            time.sleep(pause)

    if wb_errors:
        # 找到了存档记录但访问验证失败
        record["recovery_status"] = "url_found"
        record["evidence"] = (record["evidence"] or "") + \
            f"；CDX 有 {len(cdx_candidates)} 条存档但验证失败: " + " | ".join(wb_errors)
    else:
        record["recovery_status"] = "not_archived"
        record["evidence"] = (record["evidence"] or "") + "；CDX 无符合 MIME 的存档"
    return record


def fetch_snapshot_html(session: requests.Session, timestamp: str, original_url: str,
                        timeout: float, retries: int) -> dict:
    """抓取单个快照 HTML：优先 id_ 修饰符，失败退回普通回放 URL。"""
    attempts = [
        ("id_", f"https://web.archive.org/web/{timestamp}id_/{original_url}"),
        ("replay", f"https://web.archive.org/web/{timestamp}/{original_url}"),
    ]
    last_error = None
    for mode, url in attempts:
        backoff = 2.0
        for attempt in range(1, retries + 2):
            try:
                resp = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
                if resp.status_code == 429:
                    last_error = f"HTTP 429（第 {attempt} 次）"
                    logger.warning("快照 %s %s", timestamp, last_error)
                    resp.close()
                elif resp.status_code == 200:
                    raw = resp.content[:MAX_SNAPSHOT_BYTES]
                    resp.close()
                    return {"ok": True, "mode": mode, "http_status": 200,
                            "html": raw.decode("utf-8", errors="replace"), "error": None,
                            "fetched_url": url}
                else:
                    last_error = f"HTTP {resp.status_code}"
                    resp.close()
                    break  # 换下一种 URL 形式
            except requests.RequestException as exc:
                last_error = f"请求异常: {exc}"
                logger.warning("快照 %s 请求异常: %s", timestamp, exc)
            if attempt <= retries:
                time.sleep(backoff)
                backoff *= 2
    return {"ok": False, "mode": None, "http_status": None, "html": None,
            "error": last_error, "fetched_url": None}


def recover_tweet_media(session: requests.Session, tweet_id: str, snapshots: list[dict],
                        timeout: float, pause: float, retries: int,
                        cdx_cache: dict, on_progress=None) -> dict:
    """恢复单条已删除推文的媒体。单个快照/媒体失败不影响整体。"""
    ordered = sorted(snapshots, key=lambda s: s["timestamp"])
    snapshot_details = []
    found_by_snapshot = []
    text_candidates = []
    pic_links_total = 0

    for i, snap in enumerate(ordered, 1):
        if on_progress:
            on_progress({"type": "snapshot_done", "tweet_id": tweet_id,
                         "index": i, "total": len(ordered)})
        fetch = fetch_snapshot_html(session, snap["timestamp"], snap["original_url"],
                                    timeout=timeout, retries=retries)
        detail = {
            "timestamp": snap["timestamp"],
            "snapshot_url": f"https://web.archive.org/web/{snap['timestamp']}/{snap['original_url']}",
            "ok": fetch["ok"],
            "mode": fetch["mode"],
            "http_status": fetch["http_status"],
            "error": fetch["error"],
            "media_counts": {},
        }
        if fetch["ok"]:
            text = normalize_text(fetch["html"] or "")
            found = classify_and_collect(text)
            detail["media_counts"] = {k: len(v) for k, v in found.items() if v}
            detail["pic_twitter_links"] = len(PIC_LINK_RE.findall(text))
            pic_links_total += detail["pic_twitter_links"]
            found_by_snapshot.append({"timestamp": snap["timestamp"], "found": found})
            # 正文/作者恢复：按快照时间从早到晚取第一个非空值
            meta = extract_text_meta(fetch["html"] or "")
            detail["has_text_meta"] = bool(meta["text"] or meta["author"])
            if meta["text"] or meta["author"]:
                text_candidates.append({"timestamp": snap["timestamp"], **meta})
        snapshot_details.append(detail)
        if pause > 0:
            time.sleep(pause)

    recovered_text = next((c["text"] for c in text_candidates if c["text"]), None)
    recovered_author = next((c["author"] for c in text_candidates if c["author"]), None)
    text_source_timestamps = [c["timestamp"] for c in text_candidates
                              if c["text"] or c["author"]]

    merged = merge_snapshot_media(found_by_snapshot)
    media_records = []
    for j, (key, entry) in enumerate(sorted(merged.items()), 1):
        if on_progress:
            on_progress({"type": "media_done", "tweet_id": tweet_id,
                         "index": j, "total": len(merged)})
        record = recover_single_media(session, key, entry, timeout, pause, cdx_cache)
        media_records.append(record)
        if pause > 0:
            time.sleep(pause)

    recovered = sum(1 for m in media_records if m["recovery_status"] == "recovered")
    if media_count := len(media_records):
        if recovered:
            tweet_status = "recovered"
        elif any(m["recovery_status"] == "url_found" for m in media_records):
            tweet_status = "url_found"
        elif any(m["recovery_status"] == "unknown" for m in media_records):
            tweet_status = "unknown"
        else:
            tweet_status = "not_archived"
    else:
        tweet_status = "no_media_found"

    return {
        "tweet_id": str(tweet_id),
        "recovery_status": tweet_status,
        "snapshots_total": len(ordered),
        "snapshots_checked": sum(1 for d in snapshot_details if d["ok"]),
        "snapshots_failed": sum(1 for d in snapshot_details if not d["ok"]),
        "snapshot_details": snapshot_details,
        "pic_twitter_links": pic_links_total,
        "media_count": len(media_records),
        "recovered_media_count": recovered,
        "media": media_records,
        "recovered_text": recovered_text,
        "recovered_author": recovered_author,
        "text_source_timestamps": text_source_timestamps,
    }


def run_recovery(session: requests.Session, targets: list[dict],
                 pause: float = 1.0, timeout: float = 30.0, retries: int = 2,
                 on_progress=None) -> dict:
    """targets: [{"tweet_id", "snapshots": [...]}]，逐条推文处理。"""
    cdx_cache: dict = {}
    tweet_records = []
    total = len(targets)
    if on_progress:
        on_progress({"type": "start", "total": total})

    for index, target in enumerate(targets, 1):
        tweet_id = str(target["tweet_id"])
        if on_progress:
            on_progress({"type": "tweet_start", "tweet_id": tweet_id,
                         "index": index, "total": total,
                         "snapshots_total": len(target.get("snapshots") or [])})
        try:
            record = recover_tweet_media(
                session, tweet_id, target.get("snapshots") or [],
                timeout=timeout, pause=pause, retries=retries,
                cdx_cache=cdx_cache, on_progress=on_progress)
        except Exception as exc:  # 单条推文失败不影响其他推文
            logger.exception("推文 %s 恢复失败", tweet_id)
            record = {
                "tweet_id": tweet_id, "recovery_status": "unknown",
                "snapshots_total": len(target.get("snapshots") or []),
                "snapshots_checked": 0, "snapshots_failed": 0,
                "snapshot_details": [], "pic_twitter_links": 0,
                "media_count": 0, "recovered_media_count": 0, "media": [],
                "recovered_text": None, "recovered_author": None,
                "text_source_timestamps": [],
                "evidence": f"推文处理异常: {exc}",
            }
        tweet_records.append(record)
        if on_progress:
            on_progress({"type": "tweet_done", "tweet_id": tweet_id, "index": index,
                         "total": total, "recovered": record["recovered_media_count"],
                         "ok": record["recovered_media_count"] > 0
                         or record["recovery_status"] != "unknown"})

    all_media = [m for t in tweet_records for m in t["media"]]
    statistics = {
        "original_cdn_accessible": sum(1 for m in all_media if m["recovery_source"] == "original_cdn"),
        "wayback_recovered": sum(1 for m in all_media if m["recovery_source"] == "wayback"),
        "url_found": sum(1 for m in all_media if m["recovery_status"] == "url_found"),
        "not_archived": sum(1 for m in all_media if m["recovery_status"] == "not_archived"),
        "unknown": sum(1 for m in all_media if m["recovery_status"] == "unknown"),
    }
    if on_progress:
        on_progress({"type": "done"})
    return {
        "generated_at": _utc_now_iso(),
        "total_deleted_tweets": len(targets),
        "total_media_found": len(all_media),
        "total_media_recovered": statistics["original_cdn_accessible"] + statistics["wayback_recovered"],
        "statistics": statistics,
        "tweets": tweet_records,
    }


def build_targets(output_dir: Path) -> list[dict]:
    """从本地 JSON 组装恢复目标：deleted 推文 + 全部快照。零外部请求。"""
    status_path = output_dir / "x_status_results.json"
    with status_path.open(encoding="utf-8") as f:
        status_data = json.load(f)
    deleted_ids = {
        str(t["tweet_id"]) for t in status_data.get("tweets", [])
        if t.get("current_status") == "deleted"
    }
    tweets = load_tweets(output_dir)
    targets = []
    for tweet in tweets:
        if str(tweet["tweet_id"]) in deleted_ids:
            targets.append({"tweet_id": str(tweet["tweet_id"]),
                            "snapshots": tweet.get("snapshots") or []})
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复已删除推文的历史媒体")
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--pause", type=float, default=1.0, help="请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--proxy", default=None, help="显式 HTTP 代理")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    targets = build_targets(args.output)
    total_snaps = sum(len(t["snapshots"]) for t in targets)
    logger.info("待恢复推文 %d 条，历史快照共 %d 个", len(targets), total_snaps)
    if not targets:
        print("没有 deleted 推文可恢复。")
        return 0

    session = build_session(proxy=args.proxy)
    payload = run_recovery(session, targets, pause=args.pause,
                           timeout=args.timeout, retries=args.retries,
                           on_progress=lambda ev: logger.info("%s", ev))
    save_results(args.output / "media_recovery_results.json", payload)

    s = payload["statistics"]
    print(f"恢复完成: 删除推文 {payload['total_deleted_tweets']} 条 | "
          f"找到媒体 {payload['total_media_found']} | 恢复 {payload['total_media_recovered']} "
          f"(original_cdn={s['original_cdn_accessible']}, wayback={s['wayback_recovered']}, "
          f"url_found={s['url_found']}, not_archived={s['not_archived']}, unknown={s['unknown']})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
