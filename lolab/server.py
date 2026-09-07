"""本地控制台 + Timeline / State 调试页面。

独立小服务器，默认跑在 8010 端口，不影响现有的 127.0.0.1:8000。
只用标准库 http.server，只监听 127.0.0.1（外网访问不到）。

目标：让完全不用终端的人也能完成整条数据链——
在网页上填 Riot API Key、选数据目录、补下载 Timeline、逐分钟核对 State。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__, check, ddragon, net, paths, riot, state, store

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 1 * 1024 * 1024


# ---------------------------------------------------------------- 全局状态

class Console:
    """服务器运行期间的可变状态：当前数据目录 + 当前后台任务。"""

    def __init__(self, data_dir: Path) -> None:
        self.lock = threading.Lock()
        self.data_dir = data_dir
        self.state_cache: dict[str, dict[str, Any]] = {}
        self.job: dict[str, Any] = _idle_job()

    # -- 数据目录 --
    def set_data_dir(self, path: Path) -> None:
        with self.lock:
            self.data_dir = path
            self.state_cache.clear()

    def invalidate(self) -> None:
        with self.lock:
            self.state_cache.clear()

    # -- 后台任务 --
    def start_job(self, title: str, work: Callable[[Callable[[str], None]], None]) -> dict[str, Any]:
        with self.lock:
            if self.job["running"]:
                return {"started": False, "reason": "已经有一个任务在跑，等它结束。"}
            self.job = {
                "running": True, "title": title, "lines": [], "error": None,
                "done": 0, "total": 0, "finished": False,
                "startedAt": time.strftime("%H:%M:%S"),
            }

        def log(line: str) -> None:
            with self.lock:
                self.job["lines"].append(f"{time.strftime('%H:%M:%S')}  {line}")
                self.job["lines"] = self.job["lines"][-300:]

        def runner() -> None:
            try:
                work(log)
            except Exception as err:  # noqa: BLE001 - 后台任务出错必须让人看见
                with self.lock:
                    self.job["error"] = f"{type(err).__name__}: {err}"
                log(f"❌ {type(err).__name__}: {err}")
            finally:
                with self.lock:
                    self.job["running"] = False
                    self.job["finished"] = True
                self.invalidate()

        threading.Thread(target=runner, daemon=True).start()
        return {"started": True}

    def progress(self, done: int | None = None, total: int | None = None) -> None:
        with self.lock:
            if done is not None:
                self.job["done"] = done
            if total is not None:
                self.job["total"] = total

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.job, ensure_ascii=False))


def _idle_job() -> dict[str, Any]:
    return {
        "running": False, "title": "", "lines": [], "error": None,
        "done": 0, "total": 0, "finished": False, "startedAt": "",
    }


CONSOLE: Console | None = None


# ---------------------------------------------------------------- 各个动作

def api_status() -> dict[str, Any]:
    assert CONSOLE
    data_dir = CONSOLE.data_dir
    exists = data_dir.is_dir()

    key_masked, key_error = "", None
    try:
        key_masked = riot.mask(riot.load_api_key(data_dir))
    except riot.RiotError as err:
        key_error = str(err)
    key_age = riot.key_info(data_dir)

    inv = store.inventory(data_dir) if exists else {
        "matches": [], "total": 0, "withTimeline": 0,
        "withoutTimeline": 0, "unreadableJson": [], "sqliteFiles": [],
    }
    return {
        "dataDir": str(data_dir),
        "dataDirExists": exists,
        "hasKey": bool(key_masked),
        "keyMasked": key_masked,      # 永远只回遮蔽形式，绝不回完整 Key
        "keyError": key_error,
        "keyAgeHours": key_age.get("ageHours"),
        "keyExpired": key_age.get("expired"),
        "keySource": key_age.get("source"),
        "keyEnvConflict": key_age.get("envConflict"),
        "itemCostsLoaded": bool(ddragon.load(data_dir)) if exists else False,
        "version": __version__,
        "certStatus": net.describe(),
        "inventory": inv,
    }


def api_set_data_dir(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    raw = str(body.get("path", "")).strip().strip('"').strip("'")
    if not raw:
        return {"ok": False, "message": "路径是空的。"}
    path = Path(raw).expanduser()
    if not path.is_dir():
        return {"ok": False, "message": f"这个路径不存在，或者不是文件夹：{path}"}
    CONSOLE.set_data_dir(path.resolve())
    return {"ok": True, "message": f"数据目录已切换到 {path.resolve()}"}


def api_save_key(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    key = str(body.get("key", "")).strip()
    if not key:
        return {"ok": False, "message": "Key 是空的。"}
    if not key.startswith("RGAPI-"):
        return {"ok": False, "message": "看起来不像 Riot 的 Key —— 正常的 Key 以 RGAPI- 开头。"}
    data_dir = paths.ensure(CONSOLE.data_dir)
    path = riot.save_api_key(data_dir, key)
    return {"ok": True, "message": f"已保存到 {path}（文件权限已设为只有你能读）"}


def api_verify_key(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    platform = str(body.get("platform", "KR")).upper()
    try:
        key, source = riot.resolve_api_key(CONSOLE.data_dir)
    except riot.RiotError as err:
        return {"ok": False, "message": str(err)}
    used = f"（实际使用：{riot.mask(key)}，来自 {source}）"
    try:
        riot.verify_key(key, platform)
    except riot.RiotError as err:
        return {"ok": False, "message": f"{err}\n{used}"}
    return {"ok": True, "message": f"✅ Key 有效，{platform} 大区连接正常。{used}"}


def api_fetch(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    match_id = str(body.get("matchId", "")).strip()
    if not match_id:
        return {"started": False, "reason": "没有填 Match ID。"}
    overwrite = bool(body.get("overwrite"))
    data_dir = paths.ensure(CONSOLE.data_dir)
    region = str(body.get("region") or riot.guess_region(match_id))

    def work(log: Callable[[str], None]) -> None:
        key = riot.load_api_key(data_dir)
        CONSOLE.progress(0, 2)
        _fetch_one(data_dir, region, match_id, key, overwrite, log)
        CONSOLE.progress(2, 2)
        log("✅ 完成。回到「比赛库」刷新看看。")

    return CONSOLE.start_job(f"补下载 {match_id}", work)


def _fetch_one(
    data_dir: Path,
    region: str,
    match_id: str,
    key: str,
    overwrite: bool,
    log: Callable[[str], None],
) -> bool:
    """下载一场的 Match + Timeline。返回是否真的下了新东西。"""
    changed = False
    found = store.locate(data_dir, match_id)
    for kind, fetch in (("match", riot.fetch_match), ("timeline", riot.fetch_timeline)):
        if found[kind] and not overwrite:
            log(f"  ⏭  {match_id} 的 {kind} 已存在，跳过")
            continue
        obj = fetch(region, match_id, key)
        path = store.save(data_dir, kind, match_id, obj)
        changed = True
        log(f"  ✅ {match_id} {kind} → {path.name}")
    return changed


def api_sync(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    riot_id = str(body.get("riotId", "")).strip()
    if "#" not in riot_id:
        return {"started": False, "reason": "Riot ID 要写成「名字#标签」的样子，例如 JUGKING#Kr。"}
    game_name, tag_line = riot_id.split("#", 1)
    region = str(body.get("region") or "asia")
    count = max(1, min(100, int(body.get("count") or 20)))
    queue_raw = str(body.get("queue") or "420").strip()
    queue = int(queue_raw) if queue_raw.isdigit() else None
    data_dir = paths.ensure(CONSOLE.data_dir)

    def work(log: Callable[[str], None]) -> None:
        key = riot.load_api_key(data_dir)

        log(f"查询 Riot ID：{game_name}#{tag_line}（{region}）")
        account = riot.fetch_account(region, game_name, tag_line, key)
        puuid = account.get("puuid", "")
        log(f"  ✅ PUUID：{puuid[:12]}…（这才是稳定身份，改名也不变）")

        log(f"拉取最近 {count} 场" + (f"（Queue {queue}）" if queue else "（不限队列）"))
        match_ids = riot.fetch_match_ids(region, puuid, key, count, queue)
        log(f"  ✅ 拿到 {len(match_ids)} 个 Match ID")

        CONSOLE.progress(0, len(match_ids))
        downloaded = skipped = failed = 0
        for i, match_id in enumerate(match_ids, start=1):
            try:
                if _fetch_one(data_dir, region, match_id, key, False, log):
                    downloaded += 1
                else:
                    skipped += 1
            except riot.RiotError as err:
                failed += 1
                log(f"  ❌ {match_id}：{err}")
            CONSOLE.progress(i, len(match_ids))

        log("")
        log(f"完成：新下载 {downloaded} 场，已存在跳过 {skipped} 场，失败 {failed} 场。")
        inv = store.inventory(data_dir)
        log(f"现在数据目录里共 {inv['total']} 场，其中 {inv['withTimeline']} 场有 Timeline。")

    return CONSOLE.start_job(f"同步 {riot_id} 最近 {count} 场", work)


def api_items(body: dict[str, Any]) -> dict[str, Any]:
    assert CONSOLE
    data_dir = paths.ensure(CONSOLE.data_dir)

    def work(log: Callable[[str], None]) -> None:
        CONSOLE.progress(0, 1)
        version, count = ddragon.update(data_dir)
        CONSOLE.progress(1, 1)
        log(f"✅ 已缓存 {count} 件装备的价格（版本 {version}）")

    return CONSOLE.start_job("更新装备价格表", work)


def api_state(match_id: str) -> dict[str, Any]:
    assert CONSOLE
    with CONSOLE.lock:
        cached = CONSOLE.state_cache.get(match_id)
    if cached:
        return cached

    data_dir = CONSOLE.data_dir
    found = store.locate(data_dir, match_id)
    if not found["timeline"]:
        raise FileNotFoundError(
            f"本机没有找到 {match_id} 的 Timeline JSON。"
            "回到控制台首页，在「补下载」里填这个 Match ID。"
        )
    if not found["match"]:
        raise FileNotFoundError(f"本机没有找到 {match_id} 的 Match JSON。")

    built = state.build(
        found["match"][0].data, found["timeline"][0].data, ddragon.load(data_dir)
    )
    with CONSOLE.lock:
        CONSOLE.state_cache[match_id] = built
    return built


# ---------------------------------------------------------------- HTTP

class DebugHandler(BaseHTTPRequestHandler):
    server_version = "lolab"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    # -- GET --
    def do_GET(self) -> None:  # noqa: N802 - http.server 规定的方法名
        assert CONSOLE
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send_file(WEB_DIR / "console.html")
            elif path.startswith("/match/"):
                self._send_file(WEB_DIR / "debug.html")
            elif path == "/api/status":
                self._send_json(api_status())
            elif path == "/api/job":
                self._send_json(CONSOLE.snapshot())
            elif path == "/api/datadir/scan":
                self._send_json({"candidates": store.find_data_dir_candidates()})
            elif path == "/api/matches":
                self._send_json(store.inventory(CONSOLE.data_dir))
            elif path.startswith("/api/state/"):
                self._send_json(api_state(_tail(path)))
            elif path.startswith("/api/check/"):
                self._send_json(check.run(CONSOLE.data_dir, _tail(path)))
            else:
                self._send_json({"error": "not found"}, status=404)
        except FileNotFoundError as err:
            self._send_json({"error": str(err)}, status=404)
        except Exception as err:  # noqa: BLE001 - 调试服务器，错误要看得见
            self._send_json({"error": f"{type(err).__name__}: {err}"}, status=500)

    # -- POST --
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/api/datadir": api_set_data_dir,
            "/api/key": api_save_key,
            "/api/key/verify": api_verify_key,
            "/api/fetch": api_fetch,
            "/api/sync": api_sync,
            "/api/items": api_items,
        }
        handler = routes.get(path)
        if not handler:
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            self._send_json(handler(self._body()))
        except Exception as err:  # noqa: BLE001
            self._send_json({"error": f"{type(err).__name__}: {err}"}, status=500)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _tail(path: str) -> str:
    from urllib.parse import unquote

    return unquote(path.rsplit("/", 1)[-1])


def serve(data_dir: Path, port: int = 8010) -> None:
    global CONSOLE
    CONSOLE = Console(data_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), DebugHandler)
    print()
    print("  ┌──────────────────────────────────────────────┐")
    print(f"  │  控制台已启动：http://127.0.0.1:{port}        │")
    print("  └──────────────────────────────────────────────┘")
    print()
    print(f"  版本：lolab {__version__}    {net.describe()}")
    print(f"  数据目录：{data_dir}")
    print("  在 Chrome 里打开上面那个地址，接下来全在网页上点。")
    print("  要停止：回到这个窗口按 Control + C。")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
