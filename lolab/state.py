"""Timeline JSON → 分钟级 State。

这是整条数据链上最关键的一步：
    Match + Timeline  →  每一帧（默认每分钟）的完整局势快照 S_t

严格的无未来信息保证（这决定了以后训练的 V(s) 是否可信）：
第 i 帧的 events 是「第 i-1 帧到第 i 帧之间」发生的事。
所以处理顺序永远是「先累加本帧事件，再拍下本帧快照」，
任何一帧的 State 只包含该时刻及之前可观测到的信息，绝不掺入之后的击杀、
装备、经济或最终比分。唯一的例外是 meta.blueWin —— 它是训练标签，
不是 State 的一部分，特征构造时必须排除。
"""

from __future__ import annotations

from typing import Any
import json
import math

BLUE, RED = 100, 200

# 需要在 State 里逐条保留的事件类型
TRACKED_EVENTS = {
    "CHAMPION_KILL",
    "CHAMPION_SPECIAL_KILL",
    "BUILDING_KILL",
    "TURRET_PLATE_DESTROYED",
    "ELITE_MONSTER_KILL",
    "ITEM_PURCHASED",
    "ITEM_SOLD",
    "ITEM_DESTROYED",
    "ITEM_UNDO",
    "WARD_PLACED",
    "WARD_KILL",
    "LEVEL_UP",
    "SKILL_LEVEL_UP",
    "OBJECTIVE_BOUNTY_PRESTART",
    "OBJECTIVE_BOUNTY_FINISH",
    "DRAGON_SOUL_GIVEN",
    "FEAT_UPDATE",
    "GAME_END",
}


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _new_team_state() -> dict[str, Any]:
    return {
        "kills": 0,
        "deaths": 0,
        "towers": 0,
        "inhibitors": 0,
        "plates": 0,
        "dragons": 0,
        "dragonTypes": [],
        "dragonSoul": None,
        "heralds": 0,
        "voidgrubs": 0,
        "atakhans": 0,
        "barons": 0,
        "elders": 0,
        "wardsPlaced": 0,
        "wardsKilled": 0,
    }


def _new_player_state() -> dict[str, Any]:
    return {
        "kills": 0,
        "deaths": 0,
        "assists": 0,
        "wardsPlaced": 0,
        "wardsKilled": 0,
        "items": [],
        "skillPoints": {"Q": 0, "W": 0, "E": 0, "R": 0},
    }


_SKILL_SLOTS = {1: "Q", 2: "W", 3: "E", 4: "R"}


def _participant_index(match: dict[str, Any], timeline: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """把 Match 里的英雄/位置信息，按 puuid 对齐到 Timeline 的 participantId。"""
    by_puuid: dict[str, dict[str, Any]] = {}
    for entry in match.get("info", {}).get("participants", []) or []:
        puuid = entry.get("puuid")
        if puuid:
            by_puuid[puuid] = entry

    # Timeline 自带 participantId ↔ puuid 的对照表
    link = {
        int(p["participantId"]): p.get("puuid")
        for p in timeline.get("info", {}).get("participants", []) or []
        if p.get("participantId") is not None
    }

    index: dict[int, dict[str, Any]] = {}
    for pid in range(1, 11):
        puuid = link.get(pid)
        source = by_puuid.get(puuid or "", {})
        if not source:
            source = next((p for p in match.get("info", {}).get("participants", [])
                           if p.get("participantId") == pid), {})
        index[pid] = {
            "participantId": pid,
            "puuid": puuid or source.get("puuid"),
            "championName": source.get("championName"),
            "championId": source.get("championId"),
            "role": source.get("teamPosition") or source.get("individualPosition"),
            "teamId": source.get("teamId"),
            "riotIdGameName": source.get("riotIdGameName") or source.get("summonerName"),
            "riotIdTagline": source.get("riotIdTagline"),
        }
    return index


def _apply_event(
    event: dict[str, Any],
    players: dict[int, dict[str, Any]],
    teams: dict[int, dict[str, Any]],
    team_of: dict[int, int],
) -> None:
    """把一条事件累加进当前（截至此刻的）状态。"""
    kind = event.get("type")
    players = {pid: p for pid, p in players.items() if team_of.get(pid) in teams}

    if kind == "CHAMPION_KILL":
        victim = event.get("victimId")
        killer = event.get("killerId")
        if victim in players:
            players[victim]["deaths"] += 1
            teams[team_of[victim]]["deaths"] += 1
        if killer in players:
            players[killer]["kills"] += 1
            teams[team_of[killer]]["kills"] += 1
        elif killer == 0 and victim in players:
            # killerId 为 0 表示被小兵/野怪/防御塔击杀，不计入任何一方击杀数
            pass
        for assist in event.get("assistingParticipantIds") or []:
            if assist in players:
                players[assist]["assists"] += 1

    elif kind == "BUILDING_KILL":
        # teamId 是「被拆掉的建筑所属队伍」，功劳记给对面
        loser = event.get("teamId")
        if loser not in teams:
            return
        winner = RED if loser == BLUE else BLUE
        building = event.get("buildingType")
        if building == "TOWER_BUILDING":
            teams[winner]["towers"] += 1
        elif building == "INHIBITOR_BUILDING":
            teams[winner]["inhibitors"] += 1

    elif kind == "TURRET_PLATE_DESTROYED":
        loser = event.get("teamId")
        if loser not in teams:
            return
        winner = RED if loser == BLUE else BLUE
        teams[winner]["plates"] += 1

    elif kind == "ELITE_MONSTER_KILL":
        killer_team = event.get("killerTeamId")
        if killer_team not in teams:
            killer_team = team_of.get(event.get("killerId", 0))
        if killer_team not in teams:
            return
        monster = event.get("monsterType")
        if monster == "DRAGON":
            subtype = event.get("monsterSubType")
            if subtype == "ELDER_DRAGON":
                teams[killer_team].setdefault("elders", 0)
                teams[killer_team]["elders"] += 1
            else:
                teams[killer_team]["dragons"] += 1
                teams[killer_team]["dragonTypes"].append(subtype)
        elif monster == "RIFTHERALD":
            teams[killer_team]["heralds"] += 1
        elif monster == "HORDE":  # 虚空幼虫
            teams[killer_team]["voidgrubs"] += 1
        elif monster == "ATAKHAN":
            teams[killer_team]["atakhans"] += 1
        elif monster == "BARON_NASHOR":
            teams[killer_team]["barons"] += 1

    elif kind == "DRAGON_SOUL_GIVEN":
        team = event.get("teamId")
        if team in teams:
            teams[team]["dragonSoul"] = event.get("name")

    elif kind == "WARD_PLACED":
        creator = event.get("creatorId")
        if creator in players:
            players[creator]["wardsPlaced"] += 1
            teams[team_of[creator]]["wardsPlaced"] += 1

    elif kind == "WARD_KILL":
        killer = event.get("killerId")
        if killer in players:
            players[killer]["wardsKilled"] += 1
            teams[team_of[killer]]["wardsKilled"] += 1

    elif kind == "ITEM_PURCHASED":
        pid, item = event.get("participantId"), event.get("itemId")
        if pid in players and item:
            players[pid]["items"].append(item)

    elif kind in ("ITEM_SOLD", "ITEM_DESTROYED"):
        pid, item = event.get("participantId"), event.get("itemId")
        if pid in players and item in players.get(pid, {}).get("items", []):
            players[pid]["items"].remove(item)

    elif kind == "ITEM_UNDO":
        pid = event.get("participantId")
        if pid not in players:
            return
        before, after = event.get("beforeId"), event.get("afterId")
        if before and before in players[pid]["items"]:
            players[pid]["items"].remove(before)  # 撤销购买
        if after:
            players[pid]["items"].append(after)  # 撤销出售

    elif kind == "SKILL_LEVEL_UP":
        pid = event.get("participantId")
        slot = _SKILL_SLOTS.get(event.get("skillSlot"))
        if pid in players and slot:
            players[pid]["skillPoints"][slot] += 1


def build(
    match: dict[str, Any],
    timeline: dict[str, Any],
    item_costs: dict[int, int] | None = None,
) -> dict[str, Any]:
    """把一场比赛的 Match + Timeline 转换成分钟级 State 序列。"""
    item_costs = item_costs or {}
    match_info = match.get("info", {})
    tl_info = timeline.get("info", {})

    index = _participant_index(match, timeline)
    team_of = {pid: entry["teamId"] for pid, entry in index.items()}

    blue_win = None
    for team in match_info.get("teams", []) or []:
        if team.get("teamId") == BLUE:
            blue_win = team.get("win") if isinstance(team.get("win"), bool) else None

    players = {pid: _new_player_state() for pid in range(1, 11)}
    teams = {BLUE: _new_team_state(), RED: _new_team_state()}

    raw_frames = tl_info.get("frames", []) or []
    # Timestamp is authoritative; a future-dated event in an earlier frame must wait.
    pending, seen = [], set()
    for frame in raw_frames:
        for event in frame.get("events", []) or []:
            stamp = number(event.get("timestamp"))
            if stamp is None or stamp < 0:
                continue
            key = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if key not in seen:
                seen.add(key)
                pending.append(event)
    pending.sort(key=lambda event: event["timestamp"])
    cursor = 0
    frames_out: list[dict[str, Any]] = []
    valid_frames = sorted((f for f in raw_frames if number(f.get("timestamp")) is not None
                           and f["timestamp"] >= 0), key=lambda f: f["timestamp"])
    interval = number(tl_info.get("frameInterval")) or 60000
    cumulative_complete = bool(valid_frames and valid_frames[0]["timestamp"] == 0)
    previous_timestamp = None
    for frame_index, frame in enumerate(valid_frames):
        timestamp = int(frame["timestamp"])
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            continue
        cumulative_complete = cumulative_complete and isinstance(frame.get("events"), list)
        if previous_timestamp is not None and timestamp - previous_timestamp > interval * 1.5:
            cumulative_complete = False
        previous_timestamp = timestamp
        raw_events = frame.get("events", []) or []
        recent: list[dict[str, Any]] = []
        while cursor < len(pending) and pending[cursor]["timestamp"] <= timestamp:
            event = pending[cursor]
            _apply_event(event, players, teams, team_of)
            recent.append({**event, "clock": _clock(int(event["timestamp"]))})
            cursor += 1

        # 2) 再拍下这一帧的快照
        pframes = frame.get("participantFrames", {}) or {}
        players_out: list[dict[str, Any]] = []
        team_agg = {
            BLUE: {"totalGold": 0, "currentGold": 0, "xp": 0, "level": 0, "cs": 0, "jungleCs": 0, "itemGold": 0},
            RED: {"totalGold": 0, "currentGold": 0, "xp": 0, "level": 0, "cs": 0, "jungleCs": 0, "itemGold": 0},
        }
        for pid in range(1, 11):
            snapshot = pframes.get(str(pid)) or pframes.get(pid) or {}
            position = snapshot.get("position") or {}
            items = list(players[pid]["items"])
            item_gold = sum(item_costs[i] for i in items) if item_costs and all(i in item_costs for i in items) and cumulative_complete else None
            lane_cs = number(snapshot.get("minionsKilled"))
            jungle_cs = number(snapshot.get("jungleMinionsKilled"))
            entry = {
                **index[pid],
                "level": number(snapshot.get("level")),
                "xp": number(snapshot.get("xp")),
                "totalGold": number(snapshot.get("totalGold")),
                "currentGold": number(snapshot.get("currentGold")),
                "goldPerSecond": snapshot.get("goldPerSecond"),
                "cs": lane_cs + jungle_cs if lane_cs is not None and jungle_cs is not None else None,
                "laneCs": lane_cs,
                "jungleCs": jungle_cs,
                "x": position.get("x"),
                "y": position.get("y"),
                "items": items if cumulative_complete else None,
                "itemGold": item_gold,
                "kills": players[pid]["kills"],
                "deaths": players[pid]["deaths"],
                "assists": players[pid]["assists"],
                "wardsPlaced": players[pid]["wardsPlaced"],
                "wardsKilled": players[pid]["wardsKilled"],
                "skillPoints": dict(players[pid]["skillPoints"]),
                "championStats": snapshot.get("championStats"),
            }
            players_out.append(entry)

        for side in (BLUE, RED):
            members = [p for p in players_out if p["teamId"] == side]
            for key in team_agg[side]:
                values = [p.get(key) for p in members]
                team_agg[side][key] = sum(values) if len(values) == 5 and all(v is not None for v in values) else None
        if not cumulative_complete:
            for player in players_out:
                for key in ("kills", "deaths", "assists", "wardsPlaced", "wardsKilled", "skillPoints"):
                    player[key] = None

        teams_out = {
            str(side): {**_snapshot_team(teams[side]), **team_agg[side]}
            for side in (BLUE, RED)
        }
        if not cumulative_complete:
            for side in teams_out.values():
                for key in _new_team_state():
                    side[key] = None
        blue, red = teams_out[str(BLUE)], teams_out[str(RED)]
        diff = {
            key: blue[key] - red[key] if blue[key] is not None and red[key] is not None else None
            for key in ("totalGold", "xp", "level", "cs", "kills", "towers", "plates",
                        "dragons", "heralds", "voidgrubs", "barons", "elders", "atakhans", "inhibitors",
                        "wardsPlaced", "wardsKilled")
        }

        frames_out.append(
            {
                "frameIndex": frame_index,
                "timestampMs": timestamp,
                "minute": round(timestamp / 60000, 2),
                "clock": _clock(timestamp),
                "players": players_out,
                "teams": teams_out,
                "diff": diff,
                "eventCountRaw": len(raw_events),
                "cumulativeComplete": cumulative_complete,
                "recentEvents": recent,
            }
        )

    return {
        "meta": {
            "matchId": (match.get("metadata") or {}).get("matchId")
            or (timeline.get("metadata") or {}).get("matchId"),
            "gameVersion": match_info.get("gameVersion"),
            "queueId": match_info.get("queueId"),
            "platformId": match_info.get("platformId"),
            "gameDurationSec": match_info.get("gameDuration"),
            "gameCreation": match_info.get("gameCreation"),
            "frameIntervalMs": tl_info.get("frameInterval"),
            "frameCount": len(frames_out),
            "blueWin": blue_win,  # 训练标签，不属于 State 特征
            "itemCostsLoaded": bool(item_costs),
        },
        "participants": [index[pid] for pid in range(1, 11)],
        "frames": frames_out,
    }


def _snapshot_team(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    out["dragonTypes"] = list(state["dragonTypes"])
    return out


def number(value):
    """Unknown is never zero; bool, non-finite and malformed values are unknown."""
    return value if type(value) in (int, float) and math.isfinite(value) else None
