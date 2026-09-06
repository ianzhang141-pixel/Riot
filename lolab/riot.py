"""Riot API 最小客户端（只用标准库 urllib）。

只做本项目当前真正需要的事：把某一场的 Match JSON 和 Timeline JSON 抓下来。
API Key 永远只从本机文件 / 环境变量读取，绝不会被打印或写进日志。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
            with urllib.request.urlopen(request, timeout=30) as response:
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
            time.sleep(2 ** attempt)
            last = err
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
