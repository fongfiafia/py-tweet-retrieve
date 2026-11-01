"""本地调试页面服务（Python 标准库，无第三方依赖）。

- GET  /                  → static/index.html
- GET  /style.css /app.js → 静态资源
- GET  /api/data          → 合并返回 tweets.json / x_status_results.json / media_recovery_results.json
- GET  /api/inspect/progress → 检测进度
- POST /api/inspect       → 启动一次检测（不请求 Wayback CDX）
- GET  /api/recover/progress → 媒体恢复进度
- POST /api/recover       → 启动删除推文媒体恢复（不执行 Syndication）
"""

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from cdx_client import build_session
from historical_media_recovery import build_targets, run_recovery
from x_status_checker import load_tweets, run_inspection, save_results

logger = logging.getLogger("local_server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

# 检测任务共享状态（同一时间只允许一个检测任务）
INSPECT_STATE = {
    "running": False,
    "finished": True,
    "error": None,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "current_tweet_id": None,
    "last_status": None,
}

# 由 main() 注入的检测配置
INSPECT_CONFIG = {}

# 媒体恢复任务状态与配置
RECOVER_STATE = {
    "running": False,
    "finished": True,
    "error": None,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "current_tweet_id": None,
    "last_event": None,
}
RECOVER_CONFIG = {}


def _reset_state(total: int) -> None:
    INSPECT_STATE.update({
        "running": True, "finished": False, "error": None,
        "total": total, "done": 0, "success": 0, "failed": 0,
        "current_tweet_id": None, "last_status": None,
    })


def _on_progress(event: dict) -> None:
    etype = event.get("type")
    if etype == "start":
        _reset_state(event.get("total", 0))
    elif etype == "tweet_start":
        INSPECT_STATE["current_tweet_id"] = event.get("tweet_id")
    elif etype == "tweet_done":
        INSPECT_STATE["done"] = event.get("index", 0)
        INSPECT_STATE["last_status"] = event.get("status")
        if event.get("ok"):
            INSPECT_STATE["success"] += 1
        else:
            INSPECT_STATE["failed"] += 1


def _inspection_worker() -> None:
    try:
        tweets = load_tweets(INSPECT_CONFIG["output_dir"])
        INSPECT_STATE["total"] = len(tweets)
        session = build_session(proxy=INSPECT_CONFIG.get("proxy"))
        payload = run_inspection(
            session, tweets,
            pause=INSPECT_CONFIG.get("pause", 1.5),
            timeout=INSPECT_CONFIG.get("timeout", 15.0),
            retries=INSPECT_CONFIG.get("retries", 2),
            on_progress=_on_progress,
        )
        save_results(INSPECT_CONFIG["output_dir"] / "x_status_results.json", payload)
        INSPECT_STATE["finished"] = True
        logger.info("检测完成: %s", payload["statistics"])
    except Exception as exc:  # 线程内兜底，保证状态可见
        INSPECT_STATE["error"] = str(exc)
        INSPECT_STATE["finished"] = True
        logger.exception("检测任务失败")


def _on_recover_progress(event: dict) -> None:
    etype = event.get("type")
    if etype == "start":
        RECOVER_STATE.update({
            "running": True, "finished": False, "error": None,
            "total": event.get("total", 0), "done": 0,
            "success": 0, "failed": 0, "current_tweet_id": None,
            "last_event": None,
        })
    elif etype == "tweet_start":
        RECOVER_STATE["current_tweet_id"] = event.get("tweet_id")
        RECOVER_STATE["last_event"] = event
    elif etype == "tweet_done":
        RECOVER_STATE["done"] = event.get("index", 0)
        if event.get("ok"):
            RECOVER_STATE["success"] += 1
        else:
            RECOVER_STATE["failed"] += 1


def _recovery_worker() -> None:
    try:
        targets = build_targets(INSPECT_CONFIG["output_dir"])
        RECOVER_STATE["total"] = len(targets)
        if not targets:
            RECOVER_STATE["error"] = "没有 deleted 推文可恢复"
            RECOVER_STATE["finished"] = True
            return
        session = build_session(proxy=RECOVER_CONFIG.get("proxy"))
        payload = run_recovery(
            session, targets,
            pause=RECOVER_CONFIG.get("pause", 1.0),
            timeout=RECOVER_CONFIG.get("timeout", 30.0),
            retries=RECOVER_CONFIG.get("retries", 2),
            on_progress=_on_recover_progress,
        )
        save_results(INSPECT_CONFIG["output_dir"] / "media_recovery_results.json", payload)
        RECOVER_STATE["finished"] = True
        logger.info("媒体恢复完成: %s", payload["statistics"])
    except Exception as exc:
        RECOVER_STATE["error"] = str(exc)
        RECOVER_STATE["finished"] = True
        logger.exception("媒体恢复任务失败")


class Handler(BaseHTTPRequestHandler):
    server_version = "TwitterArchiveInspector/1.0"

    # ---- helpers ----
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self._send_json({"error": f"static/{name} 不存在"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_file(self, path: Path):
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            logger.warning("读取 %s 失败: %s", path, exc)
            return None

    # ---- routes ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path == "/style.css":
            self._send_file("style.css")
        elif path == "/app.js":
            self._send_file("app.js")
        elif path == "/api/data":
            self._send_json({
                "tweets_data": self._read_json_file(INSPECT_CONFIG["output_dir"] / "tweets.json"),
                "status_results": self._read_json_file(
                    INSPECT_CONFIG["output_dir"] / "x_status_results.json"),
                "recovery_results": self._read_json_file(
                    INSPECT_CONFIG["output_dir"] / "media_recovery_results.json"),
            })
        elif path == "/api/inspect/progress":
            self._send_json(dict(INSPECT_STATE))
        elif path == "/api/recover/progress":
            self._send_json(dict(RECOVER_STATE))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/inspect":
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            if INSPECT_STATE["running"] and not INSPECT_STATE["finished"]:
                self._send_json({"error": "检测正在进行中", "progress": dict(INSPECT_STATE)}, 409)
                return
            tweets_path = INSPECT_CONFIG["output_dir"] / "tweets.json"
            if not tweets_path.is_file():
                self._send_json({"error": f"缺少 {tweets_path}，请先运行 main.py"}, 400)
                return
            _reset_state(0)
            threading.Thread(target=_inspection_worker, daemon=True).start()
            self._send_json({"ok": True, "progress": dict(INSPECT_STATE)})
        elif path == "/api/recover":
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            self._handle_recover()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_recover(self) -> None:
        if RECOVER_STATE["running"] and not RECOVER_STATE["finished"]:
            self._send_json({"error": "恢复任务正在进行中", "progress": dict(RECOVER_STATE)}, 409)
            return
        status_path = INSPECT_CONFIG["output_dir"] / "x_status_results.json"
        tweets_path = INSPECT_CONFIG["output_dir"] / "tweets.json"
        for p in (status_path, tweets_path):
            if not p.is_file():
                self._send_json({"error": f"缺少 {p}，请先运行 main.py 与状态检测"}, 400)
                return
        RECOVER_STATE.update({"running": True, "finished": False, "error": None,
                              "total": 0, "done": 0, "success": 0, "failed": 0,
                              "current_tweet_id": None, "last_event": None})
        threading.Thread(target=_recovery_worker, daemon=True).start()
        self._send_json({"ok": True, "progress": dict(RECOVER_STATE)})

    def log_message(self, fmt, *args) -> None:  # 静默默认访问日志
        logger.debug("%s " + fmt, self.address_string(), *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="X/Twitter Archive Inspector 本地调试页面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--pause", type=float, default=1.5, help="检测请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--proxy", default=None, help="检测 X 用的显式 HTTP 代理")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    global INSPECT_CONFIG, RECOVER_CONFIG
    INSPECT_CONFIG = {
        "output_dir": args.output,
        "pause": args.pause,
        "timeout": args.timeout,
        "retries": args.retries,
        "proxy": args.proxy,
    }
    RECOVER_CONFIG = dict(INSPECT_CONFIG)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("页面地址: http://%s:%d", args.host, args.port)
    logger.info("数据来源: %s", args.output.resolve())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("已停止")


if __name__ == "__main__":
    main()
