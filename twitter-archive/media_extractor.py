"""从 X Syndication 返回的推文 JSON 中提取媒体资源。

只做字段解析，不下载媒体文件；展示由浏览器直接加载媒体 URL。
Syndication 为非正式接口，字段可能缺失或变化，任何单个媒体提取失败
都不应影响其他媒体和整条推文的处理。
"""

import logging

logger = logging.getLogger(__name__)


def _photo_media(item: dict) -> dict | None:
    """photo：优先原始分辨率地址（?name=orig）。"""
    base_url = item.get("media_url_https")
    if not base_url:
        return None
    original = item.get("original_info") or {}
    sizes = (item.get("sizes") or {}).get("large") or {}
    return {
        "type": "photo",
        "url": f"{base_url}?name=orig",
        "preview_image_url": None,
        "width": original.get("width") or sizes.get("w"),
        "height": original.get("height") or sizes.get("h"),
        "duration_ms": None,
    }


def _video_media(item: dict, is_gif: bool) -> dict | None:
    """video / animated_gif：选最高 bitrate 的 MP4 播放地址。"""
    info = item.get("video_info") or {}
    variants = [
        v for v in (info.get("variants") or [])
        if v.get("content_type") == "video/mp4" and v.get("url")
    ]
    best = max(variants, key=lambda v: v.get("bitrate") or 0) if variants else None
    if not best:
        return None
    original = item.get("original_info") or {}
    return {
        "type": "animated_gif" if is_gif else "video",
        "url": best["url"],
        "preview_image_url": item.get("media_url_https"),
        "width": original.get("width"),
        "height": original.get("height"),
        "duration_ms": info.get("duration_millis"),
    }


def extract_media(syndication: dict) -> list[dict]:
    """提取推文媒体列表；不存在返回空数组。"""
    media: list[dict] = []
    for item in syndication.get("mediaDetails") or []:
        if not isinstance(item, dict):
            continue
        mtype = item.get("type")
        try:
            if mtype == "photo":
                parsed = _photo_media(item)
            elif mtype == "video":
                parsed = _video_media(item, is_gif=False)
            elif mtype == "animated_gif":
                parsed = _video_media(item, is_gif=True)
            else:
                logger.debug("未知媒体类型 %r，跳过", mtype)
                parsed = None
        except Exception as exc:  # 单个媒体失败不影响其余
            logger.warning("媒体提取失败 (%s): %s", mtype, exc)
            parsed = None
        if parsed and parsed.get("url"):
            media.append(parsed)
    return media


def extract_text(syndication: dict) -> str | None:
    text = syndication.get("text")
    return text if isinstance(text, str) else None


def extract_author(syndication: dict) -> dict | None:
    user = syndication.get("user")
    if not isinstance(user, dict):
        return None
    return {
        "name": user.get("name"),
        "screen_name": user.get("screen_name"),
    }
