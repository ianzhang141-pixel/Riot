"""Riot API 最小客户端（只用标准库 urllib）。

只做本项目当前真正需要的事：把某一场的 Match JSON 和 Timeline JSON 抓下来。
API Key 永远只从本机文件 / 环境变量读取，绝不会被打印或写进日志。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import net

# Riot ID 的大区路由（Regional Route），KR 属于 asia
REGIONAL_ROUTES = ("americas", "asia", "europe", "sea")

_SECRET_FILES = ("riot_secret.json", "secret.json", "riot.json", "config.json")
_KEY_FIELDS = ("key", "api_key", "apiKey", "riot_api_key", "riotApiKey", "RIOT_API_KEY")


class RiotError(RuntimeError):
    """带 HTTP 状态码的 Riot API 错误，方便上层给出人话解释。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_api_key(data_dir: Path) -> str:
    """从环境变量或 data/riot_secret.json 读取 Development API Key。"""
    import os

    env = os.environ.get("RIOT_API_KEY")
    if env and env.strip():
        return env.strip()

    for name in _SECRET_FILES:
        path = data_dir / name
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                obj = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(obj, str) and obj.startswith("RGAPI"):
            return obj.strip()
        if isinstance(obj, dict):
            for field in _KEY_FIELDS:
                value = obj.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    raise RiotError(
        0,
        "没有找到 Riot API Key。请确认网站上的「保存到本机」已经写入 "
        f"{data_dir / 'riot_secret.json'}，或者在 Terminal 里先执行 "
        "export RIOT_API_KEY=你的Key",
    )


def get(url: str, api_key: str, retries: int = 4) -> Any:
    """GET 一个 Riot API 地址；遇到 429 限流会按 Retry-After 自动等待重试。"""
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={"X-Riot-Token": api_key})
        try:
            with net.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429:
                wait = int(err.headers.get("Retry-After", "10") or 10)
                time.sleep(min(wait, 120))
                last = err
                continue
            if err.code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                last = err
                continue
            raise RiotError(err.code, _explain(err.code, url)) from err
        except urllib.error.URLError as err:
            # 证书问题重试多少次都一样，直接给出解决办法
            if net.is_cert_error(err):
                raise RiotError(0, net.CERT_HELP) from err
            time.sleep(2 ** attempt)
            last = err
    if last is not None and net.is_cert_error(last):
        raise RiotError(0, net.CERT_HELP)
    raise RiotError(0, f"请求失败（已重试 {retries} 次）：{last}")


def _explain(status: int, url: str) -> str:
    hints = {
        400: "请求格式不对（通常是 Riot ID 或 matchId 拼错了）",
        401: "没有带 API Key",
        403: "API Key 无效或已过期。Development Key 24 小时过期，请去 Riot Developer Portal 重新生成，再在网站上重新保存一次",
        404: "Riot 说这个资源不存在（检查 matchId / Riot ID / 大区是否正确）",
        429: "触发限流，稍后再试",
    }
    return f"HTTP {status}：{hints.get(status, '未知错误')}  ({url.split('?')[0]})"


def match_url(region: str, match_id: str) -> str:
    return f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"


def timeline_url(region: str, match_id: str) -> str:
    return f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"


def fetch_match(region: str, match_id: str, api_key: str) -> dict[str, Any]:
    return get(match_url(region, match_id), api_key)


def fetch_timeline(region: str, match_id: str, api_key: str) -> dict[str, Any]:
    return get(timeline_url(region, match_id), api_key)


def guess_region(match_id: str) -> str:
    """从 matchId 前缀猜大区路由，例如 KR_8368663855 -> asia。"""
    platform = match_id.split("_", 1)[0].upper() if "_" in match_id else ""
    mapping = {
        "NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
        "KR": "asia", "JP1": "asia", "OC1": "sea", "PH2": "sea", "SG2": "sea",
        "TH2": "sea", "TW2": "sea", "VN2": "sea",
        "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe",
        "ME1": "europe",
    }
    return mapping.get(platform, "asia")


# ---------------------------------------------------------------- 更多接口

# Riot ID 大区路由 → 平台主机（Summoner/Status 这类接口要用平台主机）
PLATFORM_HOSTS = {
    "KR": "kr", "JP1": "jp1", "NA1": "na1", "BR1": "br1", "LA1": "la1",
    "LA2": "la2", "OC1": "oc1", "EUW1": "euw1", "EUN1": "eun1",
    "TR1": "tr1", "RU": "ru", "PH2": "ph2", "SG2": "sg2", "TH2": "th2",
    "TW2": "tw2", "VN2": "vn2", "ME1": "me1",
}


def verify_key(api_key: str, platform: str = "KR") -> dict[str, Any]:
    """用一个最轻量的接口验证 Key 是否有效。成功返回大区状态。"""
    host = PLATFORM_HOSTS.get(platform.upper(), "kr")
    url = f"https://{host}.api.riotgames.com/lol/status/v4/platform-data"
    return get(url, api_key, retries=1)


def fetch_account(region: str, game_name: str, tag_line: str, api_key: str) -> dict[str, Any]:
    """Riot ID（名字#标签）→ 账号信息（含 PUUID）。"""
    name = urllib.parse.quote(game_name, safe="")
    tag = urllib.parse.quote(tag_line, safe="")
    url = (
        f"https://{region}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    )
    return get(url, api_key, retries=2)


def fetch_match_ids(
    region: str,
    puuid: str,
    api_key: str,
    count: int = 20,
    queue: int | None = 420,
    start: int = 0,
) -> list[str]:
    """PUUID → 最近 N 场的 matchId 列表。queue=420 是排位 Solo/Duo。"""
    params = {"start": start, "count": max(1, min(100, count))}
    if queue:
        params["queue"] = queue
    url = (
        f"https://{region}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?"
        + urllib.parse.urlencode(params)
    )
    return get(url, api_key, retries=3)


def save_api_key(data_dir: Path, key: str) -> Path:
    """把 Key 写进 data/riot_secret.json（和现有网站用的是同一个文件）。"""
    key = key.strip()
    if not key:
        raise RiotError(0, "Key 是空的。")
    path = data_dir / "riot_secret.json"
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    existing["key"] = key
    existing["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data_dir.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)  # 只有你自己能读
    except OSError:
        pass
    return path


def mask(key: str) -> str:
    """给人看的遮蔽形式，永远不暴露完整 Key。"""
    if not key:
        return ""
    return f"{key[:9]}…{key[-4:]}" if len(key) > 16 else "已保存"
