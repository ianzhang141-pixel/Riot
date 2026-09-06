"""Data Dragon 装备价格表（用于把装备栏换算成「装备价值」）。

离线也能跑：拿不到价格表时，State 里的 itemGold 会是 None，其余字段不受影响。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_ITEMS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"


def cache_path(data_dir: Path) -> Path:
    return data_dir / "ddragon" / "item_costs.json"


def load(data_dir: Path) -> dict[int, int]:
    """读取本地缓存的 {itemId: 总价}。没有缓存就返回空表。"""
    path = cache_path(data_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    costs = raw.get("costs", raw)
    return {int(k): int(v) for k, v in costs.items() if str(k).isdigit()}


def update(data_dir: Path) -> tuple[str, int]:
    """联网抓取最新版本的装备价格并缓存。返回 (版本号, 装备数量)。"""
    with urllib.request.urlopen(_VERSIONS_URL, timeout=30) as response:
        versions = json.loads(response.read().decode("utf-8"))
    version = versions[0]

    with urllib.request.urlopen(_ITEMS_URL.format(version=version), timeout=60) as response:
        items = json.loads(response.read().decode("utf-8"))

    costs = {
        item_id: int(item.get("gold", {}).get("total", 0))
        for item_id, item in items.get("data", {}).items()
        if str(item_id).isdigit()
    }

    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"version": version, "costs": costs}, handle)
    return version, len(costs)
