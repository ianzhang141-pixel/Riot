"""Offline normalization, audited corrections, descriptive analytics and LPL gate."""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from . import state, quality
from .archive import Warehouse, digest, dumps, now

SCHEMA_VERSION = "lolab-game-v1"
PIPELINE_VERSION = "normalize-v1"


def _apply(envelope, patch, check_old=True):
    path = patch.get("path", "")
    # Small, deliberate correction surface. Adding events/identities requires a new import.
    allowed = (r"/match/info/(gameVersion|gameDuration)",
               r"/timeline/info/frames/\d+/timestamp",
               r"/timeline/info/frames/\d+/participantFrames/\d+/(totalGold|currentGold|xp|level|minionsKilled|jungleMinionsKilled)",
               r"/timeline/info/frames/\d+/participantFrames/\d+/position/(x|y)",
               r"/timeline/info/frames/\d+/events/\d+/(timestamp|killerId|victimId|killerTeamId|teamId|monsterType|monsterSubType)",
               r"/context/status")
    if not any(re.fullmatch(pattern, path) for pattern in allowed):
        raise ValueError("此字段不支持人工修改；请按文档中的字段路径填写")
    if "value" not in patch or "expected" not in patch:
        raise ValueError("修正必须提供 value 和 expected（当前值）")
    target = envelope
    keys = path.strip("/").split("/")
    try:
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        if not isinstance(target, dict):
            raise ValueError("修正只允许单个字段")
        if check_old and target.get(last) != patch["expected"]:
            raise ValueError("修正与当前数据冲突，请刷新后核对；历史修正未被静默覆盖")
        value = patch["value"]
        if value is not None and (isinstance(value, (list, dict, bool)) or
                                  type(value) not in (str, int, float)):
            raise ValueError("只允许字符串、数值或 null")
        if last not in ("gameVersion", "monsterType", "monsterSubType", "status") and value is not None and (state.number(value) is None or value < 0):
            raise ValueError("此字段必须是非负数值或 null")
        if last in ("gameVersion", "monsterType", "monsterSubType", "status") and value is not None and not isinstance(value, str):
            raise ValueError("此字段必须是字符串或 null")
        target[last] = value
    except (KeyError, IndexError, TypeError) as err:
        raise ValueError("修正路径不存在") from err


def normalize(match, timeline, context, corrections=()):
    envelope = copy.deepcopy(dict(match=match, timeline=timeline, context=context))
    for correction in corrections:
        _apply(envelope, correction["patch"])
    match, timeline, context = (envelope[k] for k in ("match", "timeline", "context"))
    if not isinstance(match.get("info"), dict) or not isinstance(timeline.get("info"), dict):
        raise ValueError("当前适配器需要完整 Match-V5 match/timeline 对象；其他来源须先添加专用适配器")
    built = state.build(match, timeline)  # No latest-version item price attached to old games.
    label = built["meta"].pop("blueWin")
    report = quality.inspect(match, timeline, built, context)
    return dict(schemaVersion=SCHEMA_VERSION, pipelineVersion=PIPELINE_VERSION,
                gameId=context["gameId"], context=context, meta=built["meta"],
                participants=built["participants"], frames=built["frames"],
                draft={"bans": {str(t.get("teamId")): t.get("bans") for t in match["info"].get("teams", [])}},
                events=[e for frame in built["frames"] for e in frame["recentEvents"]],
                labels={"blueWin": label}, quality=report)


def import_game(wh, match_response, timeline_response, context=None):
    context = copy.deepcopy(context or {})
    mr, tr = wh.response(match_response), wh.response(timeline_response)
    if not mr or not tr or mr["kind"] != "match" or tr["kind"] != "timeline" or mr["status"] != 200 or tr["status"] != 200:
        raise ValueError("需提供成功存档的 match 和 timeline 响应")
    match, timeline = json.loads(wh.raw(match_response)), json.loads(wh.raw(timeline_response))
    mid = (match.get("metadata") or {}).get("matchId")
    if not mid or mid != (timeline.get("metadata") or {}).get("matchId"):
        raise ValueError("详情和时间线比赛 ID 不一致，拒绝关联")
    context.setdefault("gameId", f"{mr['source']}:{mid}")
    context.setdefault("kind", "unclassified")
    context.setdefault("synthetic", False)
    context.setdefault("status", "completed")
    if not context.get("startTime"):
        from datetime import datetime, timezone
        info = match.get("info", {})
        started = state.number(info.get("gameStartTimestamp")) or state.number(info.get("gameCreation"))
        if started is not None and started > 0:
            context["startTime"] = datetime.fromtimestamp(started / 1000, timezone.utc).isoformat()
    corrections = wh.corrections(context["gameId"])
    if context["kind"] not in ("competitive", "soloqueue", "unclassified") or type(context["synthetic"]) is not bool:
        raise ValueError("kind 必须为 competitive / soloqueue / unclassified，synthetic 必须为布尔值")
    document = normalize(match, timeline, context, corrections)
    for source, source_id in ((mr["source"], mr["source_id"]), (tr["source"], tr["source_id"])):
        wh.add_mapping(source, source_id, context["gameId"], context.get("mappingEvidence", "unverified"))
    provenance = dict(matchResponse=match_response, timelineResponse=timeline_response,
                      matchHash=mr["sha256"], timelineHash=tr["sha256"],
                      correctionIds=[c["id"] for c in corrections], context=context,
                      sources=sorted(set([mr["source"], tr["source"]])))
    document["provenance"] = provenance
    input_hash = digest(dumps([PIPELINE_VERSION, quality.VERSION, provenance]).encode())
    document["revision"] = input_hash
    return wh.save_revision(context["gameId"], input_hash, document)


def rebuild(wh, game_id=None):
    games = [wh.game(game_id)] if game_id else wh.games()
    output = []
    for game in games:
        p = game["provenance"]
        output.append(import_game(wh, p["matchResponse"], p["timelineResponse"], p["context"]))
    return output


def correct(wh, game_id, patch, author, reason):
    if not author.strip() or not reason.strip():
        raise ValueError("修正必须填写操作者和原因")
    game = wh.game(game_id)
    p = game["provenance"]
    match, timeline = json.loads(wh.raw(p["matchResponse"])), json.loads(wh.raw(p["timelineResponse"]))
    # Serialize concurrent correction validation and insertion.
    with wh.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = [dict(r) for r in db.execute("SELECT * FROM corrections WHERE game_id=? ORDER BY id", (game_id,))]
        for c in existing:
            c["patch"] = json.loads(c["patch"])
        normalize(match, timeline, p["context"], existing + [{"patch": patch}])
        db.execute("INSERT INTO corrections(game_id,created_at,author,reason,patch) VALUES (?,?,?,?,?)",
                   (game_id, now(), author.strip(), reason.strip(), dumps(patch)))
    return rebuild(wh, game_id)[0]


def import_manifest(wh, path):
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("format") != "lolab-import-v1" or not isinstance(manifest.get("games"), list):
        raise ValueError("导入清单必须使用 lolab-import-v1 格式（见 examples/import.example.json）")
    source = manifest.get("source", "local-import")
    manifest_id = wh.capture(raw, source=source, kind="manifest", source_id=path.name,
                            endpoint=str(path), fidelity="file-bytes")
    imported, errors = [], []
    for row in manifest["games"]:
        try:
            refs = {}
            for kind in ("match", "timeline"):
                file = (path.parent / row[kind]).resolve()
                body = file.read_bytes()
                obj = json.loads(body)
                mid = obj["metadata"]["matchId"]
                refs[kind] = wh.capture(body, source=source, kind=kind, source_id=mid,
                                        endpoint=str(file), api_version=manifest.get("apiVersion", "unknown"),
                                        fidelity="file-bytes")
            context = {**row.get("context", {}), "manifestResponse": manifest_id}
            imported.append(import_game(wh, refs["match"], refs["timeline"], context)["gameId"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            errors.append({"match": row.get("match"), "error": str(err)})
    return dict(imported=imported, errors=errors)


def migrate_legacy(wh, data_dir):
    from . import store
    result = dict(imported=[], errors=[])
    for mid in store.list_match_ids(data_dir):
        found = store.locate(data_dir, mid)
        if not found["match"] or not found["timeline"]:
            result["errors"].append(dict(match=mid, error="缺少详情或时间线"))
            continue
        refs = {}
        for kind in ("match", "timeline"):
            item = found[kind][0]
            # Preserve exact existing bytes when possible; DB JSON is explicitly reserialized.
            file = Path(item.source)
            if item.source.startswith("archive:"):
                refs[kind] = int(item.source.split(":",1)[1])
                continue
            if file.is_file() and json.loads(file.read_bytes()) == item.data:
                body, fidelity = file.read_bytes(), "file-bytes"
            else:
                body, fidelity = dumps(item.data).encode(), "reserialized-json"
            latest = wh.latest_response(mid, kind)
            if latest and latest["sha256"] == digest(body):
                refs[kind] = latest["id"]
            else:
                refs[kind] = wh.capture(body, source="riot-match-v5", kind=kind, source_id=mid,
                                        endpoint=item.source, api_version="v5", fidelity=fidelity)
        try:
            # Ordinary Match-V5 imports never acquire a competitive/LPL label automatically.
            known = next((g for g in wh.games() if g["provenance"]["context"].get("gameId") == f"riot-match-v5:{mid}"), None)
            context = known["provenance"]["context"] if known else {"kind": "soloqueue"}
            result["imported"].append(import_game(wh, refs["match"], refs["timeline"], context)["gameId"])
        except (ValueError, KeyError, TypeError) as err:
            result["errors"].append(dict(match=mid, error=str(err)))
    return result


def selected_games(wh, filters=None):
    filters = filters or {}
    games = wh.games()
    for field in ("season", "league", "kind"):
        if filters.get(field):
            games = [g for g in games if str(g["context"].get(field, "")) == str(filters[field])]
    if filters.get("patch"):
        games = [g for g in games if g["meta"].get("gameVersion") == filters["patch"]]
    if filters.get("team"):
        games = [g for g in games if filters["team"].casefold() in dumps(g["context"].get("teams", {})).casefold()]
    if filters.get("player"):
        games = [g for g in games if any(filters["player"].casefold() in str(p.get("riotIdGameName") or "").casefold() for p in g["participants"])]
    return games


def at_or_before(game, timestamp):
    return next((f for f in reversed(game["frames"]) if f["timestampMs"] <= timestamp), None)


def features_at(game, timestamp):
    """Explicit whitelist, with label in a separate object. Never nearest/future frame."""
    duration = state.number(game["meta"].get("gameDurationSec"))
    if duration is not None and timestamp >= duration * 1000:
        return None
    frame = at_or_before(game, timestamp)
    if frame is None or timestamp - frame["timestampMs"] > (game["meta"].get("frameIntervalMs") or 60000) * 1.5:
        return None
    # No identities derived from post-game lane inference, GAME_END payload, or final stats.
    return dict(gameId=game["gameId"], timestampMs=frame["timestampMs"],
                patch=game["meta"].get("gameVersion"), features={k: frame["diff"].get(k)
                for k in ("totalGold", "xp", "level", "cs", "kills", "towers", "dragons", "barons", "heralds", "voidgrubs", "atakhans")})


def temporal_split(games, cutoff):
    """Whole series stays together; any series straddling cutoff belongs to test."""
    groups = defaultdict(list)
    for g in games:
        date = g["context"].get("startTime")
        if not date:
            raise ValueError("按时间切分需要每局明确的 startTime")
        groups[g["context"].get("seriesId") or g["gameId"]].append(g)
    from datetime import datetime
    def parse(value):
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            raise ValueError("时间必须包含时区")
        return d
    split = {"train": [], "test": []}
    boundary = parse(cutoff)
    for group in groups.values():
        side = "train" if max(parse(g["context"]["startTime"]) for g in group) < boundary else "test"
        split[side].extend(g["gameId"] for g in group)
    return split


def analytics(wh, filters=None):
    all_games = selected_games(wh, filters)
    eligible = [g for g in all_games if g["quality"]["ok"] and not g["context"].get("synthetic")
                and type(g["labels"]["blueWin"]) is bool]
    # Never pool different patches or competition classes silently.
    strata = defaultdict(list)
    for g in eligible:
        strata[(g["context"]["kind"], g["context"].get("league") or g["context"]["kind"], g["meta"]["gameVersion"])].append(g)
    reports = []
    for (kind, league, patch), games in sorted(strata.items()):
        conversions, bins, comebacks = [], [], []
        for minute in (10, 15, 20, 25, 30):
            observed = []
            for g in games:
                f = features_at(g, minute * 60000)
                if f is None or f["features"]["totalGold"] is None:
                    continue
                observed.append((g, f["features"]["totalGold"]))
            leads = [(g, d) for g, d in observed if d != 0]
            wins = sum((d > 0) == g["labels"]["blueWin"] for g, d in leads)
            conversions.append(dict(minute=minute, games=len(leads), leadingWins=wins,
                                    rate=wins / len(leads) if leads else None,
                                    ties=sum(d == 0 for _, d in observed)))
            for low, high in ((None, -3000), (-3000, -1000), (-1000, 0), (0, 1000), (1000, 3000), (3000, None)):
                bucket = [(g, d) for g,d in observed if (low is None or d >= low) and (high is None or d < high)]
                won = sum(g["labels"]["blueWin"] for g,d in bucket)
                bins.append(dict(minute=minute, lower=low, upper=high, games=len(bucket), blueWins=won,
                                 blueWinRate=won / len(bucket) if bucket else None))
        profiles = defaultdict(lambda: dict(games=0, wins=0, gold15=[], towers=0, dragons=0, barons=0))
        champions = defaultdict(lambda: dict(games=0, wins=0))
        players = defaultdict(lambda: dict(games=0, wins=0))
        for g in games:
            winner = 100 if g["labels"]["blueWin"] else 200
            diffs = [(f["timestampMs"], f["diff"]["totalGold"]) for f in g["frames"] if f["diff"]["totalGold"] is not None]
            adverse = [(t, -d if winner == 100 else d) for t,d in diffs]
            peak = max(adverse, key=lambda x: x[1]) if adverse else None
            if peak and peak[1] >= 3000:
                comebacks.append(dict(gameId=g["gameId"], deficit=peak[1], timestampMs=peak[0], winningSide=winner))
            for p in g["participants"]:
                won = p["teamId"] == winner
                champ = str(p.get("championName") or p.get("championId") or "unknown")
                champions[champ]["games"] += 1
                champions[champ]["wins"] += won
                key = p.get("puuid") or (g["gameId"] + ":" + str(p["participantId"]))
                players[key]["name"] = p.get("riotIdGameName")
                players[key]["games"] += 1
                players[key]["wins"] += won
            for side, team in g["context"].get("teams", {}).items():
                if side not in ("100", "200"):
                    continue
                name = team.get("id") or team.get("name")
                if not name:
                    continue
                profile = profiles[str(name)]
                profile["name"] = team.get("name")
                profile["games"] += 1
                profile["wins"] += int(side) == winner
                f = features_at(g, 900000)
                if f and f["features"]["totalGold"] is not None:
                    profile["gold15"].append(f["features"]["totalGold"] * (1 if side == "100" else -1))
                for resource in ("towers", "dragons", "barons"):
                    profile[resource] += g["frames"][-1]["teams"][side][resource] or 0
        reports.append(dict(kind=kind, league=league, patch=patch, games=len(games), conversions=conversions,
                            goldBins=bins, comebacks=comebacks, teams=dict(profiles),
                            champions=dict(champions), players=dict(players)))
    return dict(selected=len(all_games), included=len(eligible), excluded=len(all_games)-len(eligible),
                note="历史样本比例，不是当前局面预测或因果效果；零样本显示未知。按版本和赛事分组。", groups=reports)


def resource_exchanges(game, window_ms=60000):
    """Adjacent objective events within a window, descriptive only."""
    events = [e for e in game["events"] if e.get("type") in ("BUILDING_KILL", "ELITE_MONSTER_KILL")]
    return [{"startMs": e["timestamp"], "events": [x for x in events if e["timestamp"] <= x["timestamp"] <= e["timestamp"] + window_ms]}
            for e in events]


def validate_lpl(wh, attestation=None):
    attestation = attestation or {}
    candidates = [g for g in wh.games() if str(g["context"].get("league", "")).upper() == "LPL"]
    checks, qualified = [], []
    for g in candidates:
        c = g["context"]
        fields = all(c.get(k) for k in ("seriesId", "gameNumber", "season", "startTime", "mappingEvidence"))
        teams = c.get("teams", {})
        identities = all(teams.get(str(s), {}).get("id") and teams.get(str(s), {}).get("name") for s in (100, 200))
        players = all(p.get("puuid") and p.get("championId") and p.get("riotIdGameName") for p in g["participants"])
        positions = all(state.number(p.get("x")) is not None and state.number(p.get("y")) is not None for f in g["frames"] for p in f["players"])
        equipment = any(e.get("type") == "ITEM_PURCHASED" for e in g["events"])
        ok = bool(c.get("kind") == "competitive" and not c.get("synthetic") and fields and identities and players
                  and positions and equipment and g["quality"]["ok"])
        checks.append(dict(gameId=g["gameId"], ok=ok, quality=g["quality"]["ok"], mapping=bool(fields and identities),
                           playerIdentity=players, positions=positions, equipmentEvents=equipment))
        if ok:
            qualified.append(g)
    # Technical fixtures and a self-assigned LPL tag can never, by themselves, certify access/licensing.
    required = ("reviewer", "evidence", "discoveryEvidence", "historyRange", "rateLimitEvidence")
    permissions = all(isinstance(attestation.get(k), str) and attestation[k].strip() for k in required)
    for permission in ("storagePermission", "modelUsePermission"):
        reviewed = attestation.get(permission)
        permissions = permissions and isinstance(reviewed, dict) and reviewed.get("status") == "approved" and bool(reviewed.get("evidence"))
    sources = set(s for g in qualified for s in g["provenance"]["sources"])
    source_reviewed = sources.issubset(set(attestation.get("sources", [])))
    passed = len(qualified) >= 10 and permissions and source_reviewed
    result = dict(createdAt=now(), status="passed-with-manual-attestation" if passed else "blocked",
                  requiredGames=10, candidateGames=len(candidates), qualifiedGames=len(qualified),
                  seriesCount=len({g["context"]["seriesId"] for g in qualified}), checks=checks,
                  sampleGameIds=[g["gameId"] for g in qualified], attestation=attestation,
                  blockers=(["不足 10 局真实 LPL，且映射、字段、质量全部合格"] if len(qualified)<10 else [])
                  + (["缺少接口发现稳定性、历史范围、限速及保存/模型使用权限的人工核验证据"] if not permissions or not source_reviewed else []),
                  note="自动检查验证技术完整性；真实性、来源对照与使用权依赖具名人工核验，报告不代表法律授权。")
    with wh.connect() as db:
        db.execute("INSERT INTO audits(created_at,document) VALUES (?,?)", (now(), dumps(result)))
    return result
