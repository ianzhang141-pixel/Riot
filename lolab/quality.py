"""Deterministic data checks. Errors exclude a game from research aggregates."""
from collections import Counter
import json
from .state import number

VERSION = "quality-v1"


def inspect(match, timeline, built, context):
    issues = []
    def add(code, message, path="", severity="error"):
        issues.append(dict(code=code, message=message, path=path, severity=severity))
    mi, ti = match.get("info", {}), timeline.get("info", {})
    mids = [(o.get("metadata") or {}).get("matchId") for o in (match, timeline)]
    if not all(mids) or mids[0] != mids[1]:
        add("id_mismatch", "详情与时间线缺少或使用不同的比赛 ID")
    if not mi.get("gameVersion"):
        add("missing_patch", "缺少比赛版本", "/match/info/gameVersion")
    participants = mi.get("participants", [])
    ids = [p.get("participantId") for p in participants]
    if sorted(str(i) for i in ids) != sorted(str(i) for i in range(1, 11)):
        add("roster", "选手 ID 必须完整且唯一（1–10）")
    if Counter(p.get("teamId") for p in participants) != {100: 5, 200: 5}:
        add("teams", "必须能明确对应两队各五名选手")
    by_id = {p.get("participantId"): p for p in participants}
    for p in ti.get("participants", []):
        other = by_id.get(p.get("participantId"), {})
        if p.get("puuid") and other.get("puuid") and p["puuid"] != other["puuid"]:
            add("identity_mismatch", "详情与时间线的选手身份冲突")
    wins = [t.get("win") for t in mi.get("teams", [])]
    if len(wins) != 2 or not all(type(x) is bool for x in wins) or sum(wins) != 1:
        add("winner", "两队胜负缺失或不互补")
    frames = ti.get("frames", [])
    interval = number(ti.get("frameInterval"))
    if interval is None or interval <= 0:
        add("interval", "时间线采样间隔缺失或无效")
        interval = 60000
    if not frames:
        add("empty_timeline", "没有时间线帧")
    previous, previous_players, seen = None, {}, set()
    for i, f in enumerate(frames):
        path = f"/timeline/info/frames/{i}"
        stamp = number(f.get("timestamp"))
        if stamp is None or stamp < 0:
            add("timestamp", "帧时间缺失或无效", path + "/timestamp")
            continue
        if i == 0 and stamp != 0:
            add("start_gap", "时间线未从零开始，累计事件数未知", path)
        if previous is not None:
            if stamp <= previous:
                add("frame_order", "时间线倒序或重复时间戳", path)
            elif stamp - previous > interval * 1.5:
                add("frame_gap", "时间线存在采样中断", path)
        previous = stamp
        pf = f.get("participantFrames") or {}
        if set(map(str, pf)) != set(map(str, range(1, 11))):
            add("missing_players", "此帧未包含十名选手", path + "/participantFrames")
        for pid, player in pf.items():
            old = previous_players.get(str(pid), {})
            for field in ("totalGold", "currentGold", "xp", "level", "minionsKilled", "jungleMinionsKilled"):
                value = number(player.get(field))
                ppath = f"{path}/participantFrames/{pid}/{field}"
                if value is None:
                    add("missing_value", f"{field} 缺失，保留为 null", ppath)
                elif value < 0:
                    add("negative_value", f"{field} 为负数", ppath)
                elif field != "currentGold" and number(old.get(field)) is not None and value < old[field]:
                    add("decrease", f"{field} 非预期下降", ppath)
            total, current = number(player.get("totalGold")), number(player.get("currentGold"))
            if total is not None and current is not None and current > total:
                add("gold_inconsistent", "当前金币大于总金币", path)
            for axis in ("x", "y"):
                if number((player.get("position") or {}).get(axis)) is None:
                    add("missing_position", "缺少地图位置", f"{path}/participantFrames/{pid}/position/{axis}", "warning")
            previous_players[str(pid)] = player
        if not isinstance(f.get("events"), list):
            add("missing_events", "此帧 events 缺失；不能按没有事件处理", path + "/events")
        for j, event in enumerate(f.get("events", []) or []):
            ep = f"{path}/events/{j}"
            timestamp = number(event.get("timestamp"))
            if timestamp is None or timestamp < 0 or timestamp > stamp:
                add("event_time", "事件时间无效或晚于所属帧", ep + "/timestamp")
            fingerprint = json.dumps(event, sort_keys=True)
            if fingerprint in seen:
                add("duplicate_event", "完全相同的重复事件已在状态中去重", ep, "warning")
            seen.add(fingerprint)
            if event.get("type") in ("BUILDING_KILL", "TURRET_PLATE_DESTROYED") and event.get("teamId") not in (100, 200):
                add("event_team", "建筑事件缺少可靠队伍归属", ep)
            if event.get("type") == "CHAMPION_KILL" and (event.get("victimId") not in range(1, 11) or event.get("killerId") not in range(11)):
                add("event_participant", "击杀事件选手 ID 无效", ep)
            if event.get("type") == "ELITE_MONSTER_KILL":
                if event.get("monsterType") not in ("DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN"):
                    add("unknown_mechanic", "出现未支持的地图资源，需要升级机制定义", ep)
                if event.get("killerTeamId") not in (100, 200) and event.get("killerId") not in range(1, 11):
                    add("event_team", "资源事件无法确定获取方", ep)
    duration = number(mi.get("gameDuration"))
    if duration is None or duration <= 0:
        add("duration", "比赛时长缺失或无效")
    elif previous is not None and abs(duration * 1000 - previous) > interval * 1.5:
        add("end_gap", "最终帧与比赛时长不一致，可能数据中断")
    if context.get("status") in ("remake", "interrupted", "aborted"):
        add("abnormal_game", f"比赛状态：{context['status']}")
    if any(p.get("gameEndedInEarlySurrender") for p in participants):
        add("early_surrender", "存在提前投降/重赛标记，需人工核对")
    elif any(p.get("gameEndedInSurrender") for p in participants):
        add("surrender", "来源标记为投降结束", severity="warning")
    if built["frames"]:
        final = built["frames"][-1]
        for team in mi.get("teams", []):
            side = final["teams"].get(str(team.get("teamId")))
            if not side:
                continue
            objectives = team.get("objectives") or {}
            for field, objective in (("kills", "champion"), ("towers", "tower"), ("barons", "baron"), ("heralds", "riftHerald")):
                expected = (objectives.get(objective) or {}).get("kills")
                if number(expected) is not None and side[field] is not None and expected != side[field]:
                    add("final_mismatch", f"{team['teamId']} 的 {field} 累计与赛后统计不一致")
            dragon = (objectives.get("dragon") or {}).get("kills")
            if number(dragon) is not None and side["dragons"] is not None and dragon != side["dragons"] + side["elders"]:
                add("final_mismatch", "小龙与远古龙合计和赛后统计不一致")
        for p in final["players"]:
            expected = by_id.get(p["participantId"], {})
            for field in ("kills", "deaths", "assists"):
                if number(expected.get(field)) is not None and p[field] is not None and expected[field] != p[field]:
                    add("player_final_mismatch", f"选手 {p['participantId']} 的 {field} 与赛后统计不一致")
    counts = Counter(i["severity"] for i in issues)
    return dict(version=VERSION, ok=not counts["error"], errors=counts["error"], warnings=counts["warning"], issues=issues,
                maxGapMs=max([b["timestamp"] - a["timestamp"] for a,b in zip(frames,frames[1:])
                              if number(a.get("timestamp")) is not None and number(b.get("timestamp")) is not None] or [0]))
