"""Timeline / State 调试页面（独立小服务器，不影响现有的 127.0.0.1:8000）。

只用标准库 http.server，默认跑在 8010 端口，
目的只有一个：让人用肉眼确认从 Timeline 抽出来的 State 是不是对的。
"""

from __future__ import annotations

import json
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import check, ddragon, state, store

WEB_DIR = Path(__file__).parent / "web"


class DebugHandler(BaseHTTPRequestHandler):
    data_dir: Path

    # 缓存已经算好的 State，避免每次拖动滑块都重新解析几 MB 的 Timeline
    _cache: dict[str, dict[str, Any]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server 规定的方法名
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/match/"):
                self._send_file(WEB_DIR / "debug.html", "text/html; charset=utf-8")
            elif path == "/api/matches":
                self._send_json({"matches": store.list_match_ids(self.data_dir)})
            elif path.startswith("/api/state/"):
                self._send_json(self._state(path.rsplit("/", 1)[-1]))
            elif path.startswith("/api/check/"):
                self._send_json(check.run(self.data_dir, path.rsplit("/", 1)[-1]))
            else:
                self._send_json({"error": "not found"}, status=404)
        except FileNotFoundError as err:
            self._send_json({"error": str(err)}, status=404)
        except Exception as err:  # noqa: BLE001 - 调试服务器，错误要看得见
            self._send_json({"error": f"{type(err).__name__}: {err}"}, status=500)

    def _state(self, match_id: str) -> dict[str, Any]:
        if match_id in self._cache:
            return self._cache[match_id]
        found = store.locate(self.data_dir, match_id)
        if not found["timeline"]:
            raise FileNotFoundError(
                f"本机没有找到 {match_id} 的 Timeline JSON。"
                f"先执行：python3 -m lolab fetch {match_id}"
            )
        if not found["match"]:
            raise FileNotFoundError(f"本机没有找到 {match_id} 的 Match JSON。")
        built = state.build(
            found["match"][0].data,
            found["timeline"][0].data,
            ddragon.load(self.data_dir),
        )
        self._cache[match_id] = built
        return built

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(data_dir: Path, port: int = 8010) -> None:
    handler = functools.partial(DebugHandler)
    DebugHandler.data_dir = data_dir
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"调试页面已启动：http://127.0.0.1:{port}")
    print(f"数据目录：{data_dir}")
    print("按 Control + C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
