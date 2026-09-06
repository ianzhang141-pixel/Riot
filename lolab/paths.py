"""数据目录定位。"""

from __future__ import annotations

import os
from pathlib import Path

# 常见的项目数据目录名，从当前目录逐级向上查找
_CANDIDATES = ("data", "Data", "storage", "var")


def find_data_dir(explicit: str | None = None) -> Path:
    """返回项目的 data 目录。

    优先级：命令行 --data > 环境变量 LOLAB_DATA > 从当前目录向上找 data/ > ./data
    找不到时返回 ./data（并在需要时由调用方创建）。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("LOLAB_DATA")
    if env:
        return Path(env).expanduser().resolve()

    here = Path.cwd().resolve()
    for base in (here, *here.parents):
        for name in _CANDIDATES:
            candidate = base / name
            if candidate.is_dir():
                return candidate
        # 到达 home 或文件系统根目录就停下，不要一路扫到 /
        if base == Path.home() or base.parent == base:
            break

    return here / "data"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
