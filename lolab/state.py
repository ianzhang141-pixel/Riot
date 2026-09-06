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
            # 没有 puuid 对照时退回按顺序对齐（participantId 1-10 == participants[0-9]）
            plist = match.get("info", {}).get("participants", []) or []
            source = plist[pid - 1] if len(plist) >= pid else {}
        index[pid] = {
            "participantId": pid,
            "puuid": puuid or source.get("puuid"),
            "championName": source.get("championName"),
            "championId": source.get("championId"),
            "role": source.get("teamPosition") or source.get("individualPosition"),
            "teamId": source.get("teamId") or (BLUE if pid <= 5 else RED),
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
        winner = RED if loser == BLUE else BLUE
        building = event.get("buildingType")
        if building == "TOWER_BUILDING":
            teams[winner]["towers"] += 1
        elif building == "INHIBITOR_BUILDING":
            teams[winner]["inhibitors"] += 1

    elif kind == "TURRET_PLATE_DESTROYED":
        loser = event.get("teamId")
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
            blue_win = bool(team.get("win"))

    players = {pid: _new_player_state() for pid in range(1, 11)}
    teams = {BLUE: _new_team_state(), RED: _new_team_state()}

    frames_out: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(tl_info.get("frames", []) or []):
        timestamp = int(frame.get("timestamp", 0))

        # 1) 先累加「这一帧之前」发生的事件
        raw_events = frame.get("events", []) or []
        recent: list[dict[str, Any]] = []
        for event in raw_events:
            _apply_event(event, players, teams, team_of)
            if event.get("type") in TRACKED_EVENTS:
                recent.append({**event, "clock": _clock(int(event.get("timestamp", timestamp)))})

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
            item_gold = sum(item_costs.get(i, 0) for i in items) if item_costs else None
            lane_cs = int(snapshot.get("minionsKilled", 0) or 0)
            jungle_cs = int(snapshot.get("jungleMinionsKilled", 0) or 0)
            entry = {
                **index[pid],
                "level": int(snapshot.get("level", 0) or 0),
                "xp": int(snapshot.get("xp", 0) or 0),
                "totalGold": int(snapshot.get("totalGold", 0) or 0),
                "currentGold": int(snapshot.get("currentGold", 0) or 0),
                "goldPerSecond": snapshot.get("goldPerSecond"),
                "cs": lane_cs + jungle_cs,
                "laneCs": lane_cs,
                "jungleCs": jungle_cs,
                "x": position.get("x"),
                "y": position.get("y"),
                "items": items,
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

            side = team_agg[team_of[pid]]
            side["totalGold"] += entry["totalGold"]
            side["currentGold"] += entry["currentGold"]
            side["xp"] += entry["xp"]
            side["level"] += entry["level"]
            side["cs"] += entry["cs"]
            side["jungleCs"] += entry["jungleCs"]
            if item_gold is not None:
                side["itemGold"] += item_gold

        teams_out = {
            str(side): {**_snapshot_team(teams[side]), **team_agg[side]}
            for side in (BLUE, RED)
        }
        blue, red = teams_out[str(BLUE)], teams_out[str(RED)]
        diff = {
            key: blue[key] - red[key]
            for key in ("totalGold", "xp", "level", "cs", "kills", "towers", "plates",
                        "dragons", "heralds", "voidgrubs", "barons", "inhibitors",
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
