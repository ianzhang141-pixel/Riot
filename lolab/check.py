"""Timeline 数据完整性检查。

回答一个非常具体的问题：某一场比赛的 Match JSON 和 Timeline JSON
到底有没有真的存在本机，以及里面的字段是不是真的够用来做 State。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import store

# 做 State 必须逐帧存在的 participantFrame 字段
REQUIRED_PLAYER_FIELDS = (
    "level",
    "xp",
    "totalGold",
    "currentGold",
    "minionsKilled",
    "jungleMinionsKilled",
    "position",
)

# 期望在一场完整对局里出现的事件类型
EXPECTED_EVENTS = (
    "CHAMPION_KILL",
    "ITEM_PURCHASED",
    "WARD_PLACED",
    "LEVEL_UP",
    "SKILL_LEVEL_UP",
    "BUILDING_KILL",
    "ELITE_MONSTER_KILL",
)


def inspect_timeline(timeline: dict[str, Any]) -> dict[str, Any]:
    info = timeline.get("info", {}) or {}
    frames = info.get("frames", []) or []

    missing_fields: Counter[str] = Counter()
    incomplete_frames: list[int] = []
    frames_without_position: list[int] = []
    event_types: Counter[str] = Counter()
    total_events = 0

    for i, frame in enumerate(frames):
        pframes = frame.get("participantFrames", {}) or {}
        present = {str(k) for k in pframes}
        if not {str(n) for n in range(1, 11)} <= present:
            incomplete_frames.append(i)

        for pid in range(1, 11):
            snapshot = pframes.get(str(pid)) or pframes.get(pid)
            if not snapshot:
                continue
            for field in REQUIRED_PLAYER_FIELDS:
                if field not in snapshot:
                    missing_fields[field] += 1
            position = snapshot.get("position") or {}
            if position.get("x") is None or position.get("y") is None:
                if i not in frames_without_position:
                    frames_without_position.append(i)

        for event in frame.get("events", []) or []:
            total_events += 1
            event_types[event.get("type", "UNKNOWN")] += 1

    last_ms = int(frames[-1].get("timestamp", 0)) if frames else 0
    return {
        "frameCount": len(frames),
        "frameIntervalMs": info.get("frameInterval"),
        "lastFrameMs": last_ms,
        "coveredMinutes": round(last_ms / 60000, 1),
        "participantLinks": len(info.get("participants", []) or []),
        "incompleteFrames": incomplete_frames,
        "framesWithoutPosition": frames_without_position,
        "missingFields": dict(missing_fields),
        "eventTypes": dict(event_types.most_common()),
        "totalEvents": total_events,
        "missingExpectedEvents": [e for e in EXPECTED_EVENTS if not event_types.get(e)],
    }


def run(data_dir: Path, match_id: str) -> dict[str, Any]:
    found = store.locate(data_dir, match_id)
    report: dict[str, Any] = {
        "dataDir": str(data_dir),
        "matchId": match_id,
        "matchSources": [f.source for f in found["match"]],
        "timelineSources": [f.source for f in found["timeline"]],
        "hasMatch": bool(found["match"]),
        "hasTimeline": bool(found["timeline"]),
    }

    if found["match"]:
        info = found["match"][0].data.get("info", {}) or {}
        report["match"] = {
            "gameVersion": info.get("gameVersion"),
            "queueId": info.get("queueId"),
            "gameDurationSec": info.get("gameDuration"),
            "participants": len(info.get("participants", []) or []),
        }

    if found["timeline"]:
        report["timeline"] = inspect_timeline(found["timeline"][0].data)

    report["verdict"] = _verdict(report)
    return report


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if not report["hasMatch"]:
        problems.append("没有找到 Match JSON")
    if not report["hasTimeline"]:
        problems.append("没有找到 Timeline JSON —— 这就是当前最大的技术断点")

    tl = report.get("timeline")
    if tl:
        if tl["frameCount"] < 5:
            problems.append(f"Timeline 只有 {tl['frameCount']} 帧，明显不完整")
        if tl["incompleteFrames"]:
            problems.append(f"有 {len(tl['incompleteFrames'])} 帧不足 10 名玩家")
        if tl["framesWithoutPosition"]:
            problems.append(f"有 {len(tl['framesWithoutPosition'])} 帧缺少 x/y 坐标")
        if tl["missingFields"]:
            problems.append(f"缺少字段：{', '.join(tl['missingFields'])}")
        if tl["missingExpectedEvents"]:
            problems.append(f"缺少事件类型：{', '.join(tl['missingExpectedEvents'])}")
        match_duration = (report.get("match") or {}).get("gameDurationSec")
        if match_duration and tl["lastFrameMs"]:
            gap = match_duration - tl["lastFrameMs"] / 1000
            if gap > 120:
                problems.append(
                    f"Timeline 只覆盖到 {tl['coveredMinutes']} 分钟，但这场打了 "
                    f"{round(match_duration / 60, 1)} 分钟，疑似被截断"
                )

    return {"ok": not problems, "problems": problems}


def format_text(report: dict[str, Any]) -> str:
    """把检查结果排版成人能直接读的中文报告。"""
    lines: list[str] = []
    tick = lambda ok: "✅" if ok else "❌"

    lines.append("=" * 62)
    lines.append(f"Timeline 数据完整性检查：{report['matchId']}")
    lines.append(f"数据目录：{report['dataDir']}")
    lines.append("=" * 62)

    lines.append(f"{tick(report['hasMatch'])} Match JSON")
    for src in report["matchSources"]:
        lines.append(f"      来源：{src}")
    if report.get("match"):
        m = report["match"]
        lines.append(
            f"      版本 {m['gameVersion']} · Queue {m['queueId']} · "
            f"时长 {round((m['gameDurationSec'] or 0) / 60, 1)} 分钟 · "
            f"{m['participants']} 名玩家"
        )

    lines.append(f"{tick(report['hasTimeline'])} Timeline JSON")
    for src in report["timelineSources"]:
        lines.append(f"      来源：{src}")

    tl = report.get("timeline")
    if tl:
        lines.append("")
        lines.append("  —— Timeline 内容 ——")
        lines.append(
            f"      帧数：{tl['frameCount']}  帧间隔：{tl['frameIntervalMs']} 毫秒  "
            f"覆盖到：{tl['coveredMinutes']} 分钟"
        )
        lines.append(f"      participantId ↔ puuid 对照：{tl['participantLinks']} 条")
        lines.append(
            f"      {tick(not tl['incompleteFrames'])} 每帧 10 名玩家"
            + (f"（异常帧 {tl['incompleteFrames'][:10]}）" if tl["incompleteFrames"] else "")
        )
        lines.append(
            f"      {tick(not tl['framesWithoutPosition'])} 每帧 x/y 坐标"
            + (f"（异常帧 {tl['framesWithoutPosition'][:10]}）" if tl["framesWithoutPosition"] else "")
        )
        lines.append(
            f"      {tick(not tl['missingFields'])} 关键字段 "
            f"level / xp / totalGold / currentGold / cs / jungleCs / position"
        )
        lines.append("")
        lines.append(f"  —— 事件（共 {tl['totalEvents']} 条）——")
        for name, count in tl["eventTypes"].items():
            lines.append(f"      {name:<32} {count}")

    lines.append("")
    lines.append("-" * 62)
    verdict = report["verdict"]
    if verdict["ok"]:
        lines.append("结论：✅ 数据完整，可以进入下一步（生成 State / 做胜率模型）。")
    else:
        lines.append("结论：❌ 有问题，先解决下面这些：")
        for problem in verdict["problems"]:
            lines.append(f"      · {problem}")
        if not report["hasTimeline"]:
            lines.append("")
            lines.append(
                f"      建议执行：python3 -m lolab fetch {report['matchId']}"
            )
    lines.append("-" * 62)
    return "\n".join(lines)
