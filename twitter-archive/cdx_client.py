"""CDX API 客户端：请求 Wayback Machine CDX 接口，处理重试与 resumeKey 分页。"""

import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"

# 需要重试的瞬态错误状态码
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

CDX_FIELDS = "timestamp,original,statuscode,mimetype,digest"


def build_session(max_retries: int = 4, backoff_factor: float = 2.0,
                  proxy: str | None = None) -> requests.Session:
    """创建带 Retry 机制的 Session。

    对 429/500/502/503/504 以及连接错误做指数退避重试。
    指定 proxy 时显式走该代理，并忽略环境变量里的其他代理设置。
    """
    session = requests.Session()
    if proxy:
        session.trust_env = False
        session.proxies = {"http": proxy, "https": proxy}
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "twitter-archive-cli/1.0 (research tool)"})
    return session


def query_cdx(session: requests.Session, url_pattern: str, timeout: float, pause: float) -> dict:
    """查询单个 URL 模式的全部 CDX 记录（含 resumeKey 分页）。

    返回 dict:
      - pattern: 查询的 URL 模式
      - rows: list[dict]，每条含 timestamp/original/statuscode/mimetype/digest
      - row_count: 记录条数
      - error: 出错信息（无错为 None）
      - pages: 实际请求的页数（用于判断是否发生分页）
      - truncated: 是否因分页截断后未能取回全部数据
    """
    result = {
        "pattern": url_pattern,
        "rows": [],
        "row_count": 0,
        "error": None,
        "pages": 0,
        "truncated": False,
    }
    base_params = {
        "url": url_pattern,
        "output": "json",
        "fl": CDX_FIELDS,
        "filter": "statuscode:200",
        "collapse": "digest",
        "showResumeKey": "true",
    }

    resume_key = None
    while True:
        params = dict(base_params)
        if resume_key:
            params["resumeKey"] = resume_key

        try:
            resp = session.get(CDX_ENDPOINT, params=params, timeout=timeout)
        except requests.RequestException as exc:
            result["error"] = f"请求失败: {exc}"
            logger.error("CDX 查询 %s 失败: %s", url_pattern, exc)
            return result

        if resp.status_code == 404:
            # 该模式没有任何存档记录
            logger.info("CDX 查询 %s 无记录 (404)", url_pattern)
            return result
        if resp.status_code != 200:
            result["error"] = f"CDX API 返回 HTTP {resp.status_code}: {resp.text[:300]}"
            logger.error("CDX 查询 %s 返回 HTTP %s", url_pattern, resp.status_code)
            return result

        try:
            data = resp.json()
        except ValueError as exc:
            result["error"] = f"CDX 返回非 JSON 内容: {exc}; body[:200]={resp.text[:200]!r}"
            logger.error(result["error"])
            return result

        if not isinstance(data, list) or not data:
            return result

        # JSON 输出的第一行是表头，最后一行可能是 resumeKey
        header = data[0]
        body_rows = data[1:]
        next_resume_key = None
        if body_rows and isinstance(body_rows[-1], list) and len(body_rows[-1]) == 1:
            # 末尾单独一行 [resume_key]（或空串）表示截断标记
            tail = body_rows[-1][0]
            body_rows = body_rows[:-1]
            if tail:
                next_resume_key = tail

        for raw in body_rows:
            if not isinstance(raw, list) or len(raw) != len(header):
                logger.warning("跳过格式异常的 CDX 行: %r", raw)
                continue
            result["rows"].append(dict(zip(header, raw)))

        result["pages"] += 1
        logger.info(
            "CDX 查询 %s 第 %d 页取回 %d 行（累计 %d）",
            url_pattern, result["pages"], len(body_rows), len(result["rows"]),
        )

        if next_resume_key:
            resume_key = next_resume_key
            if pause > 0:
                time.sleep(pause)
            continue

        # 没有 resumeKey，说明数据已完整取回
        result["row_count"] = len(result["rows"])
        return result
