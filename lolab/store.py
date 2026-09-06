"""在项目 data/ 目录里寻找 / 保存 Match 与 Timeline JSON。

现有项目的目录结构未知，所以这里不假设任何布局：
扫描 data/ 下所有 .json，按内容判断是 Match 还是 Timeline。
同时也会扫描 SQLite 文件，因为比赛数据也可能存在数据库里。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# 单个 JSON 超过这个大小就不当作候选（Timeline 一般 1~6 MB）
_MAX_JSON_BYTES = 200 * 1024 * 1024


@dataclass
class Found:
    """一次命中：某个 matchId 的 Match 或 Timeline 数据。"""

    kind: str  # "match" | "timeline"
    match_id: str
    source: str  # 文件路径，或 "sqlite:<file>:<table>:<rowid>"
    data: dict[str, Any] = field(repr=False, default_factory=dict)


def classify(obj: Any) -> str | None:
    """判断一个已解析的 JSON 对象是 Match 还是 Timeline。"""
    if not isinstance(obj, dict):
        return None
    info = obj.get("info")
    if not isinstance(info, dict):
        return None
    if isinstance(info.get("frames"), list):
        return "timeline"
    if isinstance(info.get("participants"), list) and "gameDuration" in info:
        return "match"
    return None


def match_id_of(obj: dict[str, Any]) -> str | None:
    meta = obj.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("matchId"), str):
        return meta["matchId"]
    return None


def _iter_json_files(data_dir: Path) -> Iterator[Path]:
    for path in sorted(data_dir.rglob("*.json")):
        try:
            if path.stat().st_size > _MAX_JSON_BYTES:
                continue
        except OSError:
            continue
        yield path


def _unwrap(obj: Any) -> Iterator[Any]:
    """有些项目会把 Riot 返回值包一层，例如 {"match": {...}, "timeline": {...}}。"""
    yield obj
    if isinstance(obj, dict):
        for key in ("match", "timeline", "data", "raw", "payload", "json", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict):
                yield inner


def scan_json(data_dir: Path, match_id: str | None = None) -> list[Found]:
    """扫描 data/ 下的 JSON 文件。match_id 为 None 时返回全部命中。"""
    out: list[Found] = []
    for path in _iter_json_files(data_dir):
        # 先用文件名快速过滤，命中不了再看内容（避免整目录反复解析大文件）
        if match_id and match_id not in path.name and match_id not in str(path.parent):
            # 文件名不含 matchId 时仍然解析，但只解析较小的文件
            try:
                if path.stat().st_size > 32 * 1024 * 1024:
                    continue
            except OSError:
                continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                obj = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        for candidate in _unwrap(obj):
            kind = classify(candidate)
            if not kind:
                continue
            mid = match_id_of(candidate) or _guess_id_from_name(path)
            if not mid:
                continue
            if match_id and mid != match_id:
                continue
            out.append(Found(kind=kind, match_id=mid, source=str(path), data=candidate))
            break
    return out


def _guess_id_from_name(path: Path) -> str | None:
    """文件里没有 metadata.matchId 时，从文件名里猜（例如 KR_8368663855.json）。"""
    import re

    hit = re.search(r"\b([A-Z]{2,5}\d?_\d{6,})\b", path.name)
    return hit.group(1) if hit else None


def scan_sqlite(data_dir: Path, match_id: str) -> list[Found]:
    """扫描 data/ 下的 SQLite 文件，看看比赛数据是不是存在数据库里。"""
    out: list[Found] = []
    db_files = [
        p
        for pattern in ("*.db", "*.sqlite", "*.sqlite3")
        for p in data_dir.rglob(pattern)
    ]
    for db_path in sorted(set(db_files)):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            conn.text_factory = str
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for table in tables:
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                except sqlite3.Error:
                    continue
                if not cols:
                    continue
                where = " OR ".join(f'CAST("{c}" AS TEXT) LIKE ?' for c in cols)
                params = [f"%{match_id}%"] * len(cols)
                try:
                    rows = conn.execute(
                        f'SELECT rowid, * FROM "{table}" WHERE {where} LIMIT 20', params
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    rowid, values = row[0], row[1:]
                    for col, value in zip(cols, values):
                        if not isinstance(value, (str, bytes)) or len(value) < 200:
                            continue
                        try:
                            text = value.decode("utf-8") if isinstance(value, bytes) else value
                            obj = json.loads(text)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        kind = classify(obj)
                        if not kind:
                            continue
                        out.append(
                            Found(
                                kind=kind,
                                match_id=match_id_of(obj) or match_id,
                                source=f"sqlite:{db_path}:{table}.{col}:rowid={rowid}",
                                data=obj,
                            )
                        )
        finally:
            conn.close()
    return out


def locate(data_dir: Path, match_id: str) -> dict[str, list[Found]]:
    """返回 {"match": [...], "timeline": [...]}，包含 JSON 和 SQLite 两种来源。"""
    found = scan_json(data_dir, match_id) + scan_sqlite(data_dir, match_id)
    grouped: dict[str, list[Found]] = {"match": [], "timeline": []}
    for item in found:
        grouped[item.kind].append(item)
    return grouped


def list_match_ids(data_dir: Path) -> list[str]:
    """列出 data/ 里所有能找到的 matchId（有 Timeline 的排在前面）。"""
    found = scan_json(data_dir)
    with_timeline = {f.match_id for f in found if f.kind == "timeline"}
    all_ids = {f.match_id for f in found}
    return sorted(with_timeline) + sorted(all_ids - with_timeline)


def save(data_dir: Path, kind: str, match_id: str, obj: dict[str, Any]) -> Path:
    """把 Match / Timeline 写入 data/matches/ 或 data/timelines/。"""
    sub = "matches" if kind == "match" else "timelines"
    target_dir = data_dir / sub
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".json" if kind == "match" else ".timeline.json"
    path = target_dir / f"{match_id}{suffix}"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False)
    return path


# ---------------------------------------------------------------- 快速盘点

# 只读文件开头这么多字节来判断类型，避免为了列个清单去解析几百 MB 的 Timeline
_PEEK_BYTES = 16 * 1024


def _peek_kind(path: Path) -> str | None:
    """只读文件开头，快速判断是 Match 还是 Timeline。判断不了返回 None。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(_PEEK_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    if '"frameInterval"' in head or '"frames"' in head or '"participantFrames"' in head:
        return "timeline"
    if '"gameDuration"' in head or '"gameCreation"' in head:
        return "match"
    return None


def inventory(data_dir: Path) -> dict[str, Any]:
    """盘点 data/ 里有哪些比赛、每场的 Match / Timeline 齐不齐。

    为了快，优先用「文件名 + 开头几 KB」判断；判断不出来的才整份解析。
    """
    matches: dict[str, str] = {}   # matchId -> 文件路径
    timelines: dict[str, str] = {}
    unknown: list[str] = []

    for path in _iter_json_files(data_dir):
        match_id = _guess_id_from_name(path)
        kind = _peek_kind(path)

        if match_id is None or kind is None:
            # 文件名或开头看不出来，才老老实实整份解析
            try:
                with path.open("r", encoding="utf-8") as handle:
                    obj = json.load(handle)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            for candidate in _unwrap(obj):
                kind = classify(candidate)
                if kind:
                    match_id = match_id_of(candidate) or match_id
                    break
            if not kind or not match_id:
                unknown.append(str(path))
                continue

        target = timelines if kind == "timeline" else matches
        target.setdefault(match_id, str(path))

    db_files = sorted(
        {
            str(p)
            for pattern in ("*.db", "*.sqlite", "*.sqlite3")
            for p in data_dir.rglob(pattern)
        }
    )

    all_ids = sorted(set(matches) | set(timelines), reverse=True)
    return {
        "matches": [
            {
                "matchId": mid,
                "hasMatch": mid in matches,
                "hasTimeline": mid in timelines,
            }
            for mid in all_ids
        ],
        "total": len(all_ids),
        "withTimeline": sum(1 for mid in all_ids if mid in timelines),
        "withoutTimeline": sum(1 for mid in all_ids if mid not in timelines),
        "unreadableJson": unknown[:20],
        "sqliteFiles": db_files,
    }


def find_data_dir_candidates(home: Path | None = None, max_depth: int = 5) -> list[dict[str, Any]]:
    """在个人文件夹里找可能是项目 data 目录的地方，供网页上一键选择。"""
    home = home or Path.home()
    skip = {
        "Library", "Applications", "Pictures", "Music", "Movies",
        "node_modules", "__pycache__", "venv", ".venv", "site-packages",
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(directory.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
                continue
            if entry.name in _CANDIDATE_NAMES:
                key = str(entry.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(_describe_candidate(entry))
            walk(entry, depth + 1)

    walk(home, 0)
    out.sort(key=lambda c: (-c["score"], c["path"]))
    return out[:20]


_CANDIDATE_NAMES = {"data", "Data", "storage", "var"}


def _describe_candidate(directory: Path) -> dict[str, Any]:
    """给一个候选目录打分：有 riot_secret.json 的最像，其次是有一堆 JSON 的。"""
    has_secret = (directory / "riot_secret.json").is_file()
    json_count = 0
    try:
        for path in directory.rglob("*.json"):
            json_count += 1
            if json_count >= 500:
                break
    except (OSError, PermissionError):
        pass
    score = (100 if has_secret else 0) + min(json_count, 50)
    return {
        "path": str(directory),
        "hasSecret": has_secret,
        "jsonCount": json_count,
        "score": score,
    }
