"""Optional PandaScore fixture discovery; fixture access is NOT timeline access.

No undocumented Riot esports endpoint, shared key or invented GRID schema.
PandaScore calls a tournament edition `serie`; our seriesId means one BO3/BO5 match.
"""
import json
import os
import urllib.parse
from .archive import dumps
from .transport import Client


def discover(wh, config, max_pages=20, client=None):
    if config.get("provider") != "pandascore":
        raise ValueError("目前仅支持 PandaScore 赛程发现；普通 Riot Key 不提供已验证的 LPL 赛程发现")
    league_id = config.get("leagueId")
    if type(league_id) is not int or league_id <= 0:
        raise ValueError("请填写从来源核实的 LPL leagueId；系统不会猜测 ID")
    token = os.environ.get("PANDASCORE_TOKEN", "").strip()
    if client is None:
        if not token:
            raise ValueError("尚未配置 PANDASCORE_TOKEN；Riot API Key 不能替代它")
        client = Client(wh, "pandascore", "v2", {"Authorization": f"Bearer {token}"})
    if not 1 <= max_pages <= 100:
        raise ValueError("max_pages 必须在 1–100 之间")
    checkpoint = f"pandascore-lpl-{league_id}"
    with wh.connect() as db:
        row = db.execute("SELECT value FROM checkpoints WHERE name=?", (checkpoint,)).fetchone()
    page = int(row[0]) if row else 1
    saved = 0
    for _ in range(max_pages):
        query = urllib.parse.urlencode({"filter[league_id]": league_id, "sort": "id", "page[number]": page, "page[size]": 100})
        ref, fixtures = client.fetch("https://api.pandascore.co/lol/matches?" + query,
                                     "schedule", f"league-{league_id}-page-{page}", refresh=True)
        if not isinstance(fixtures, list):
            raise ValueError("赛程返回结构已变化；保留原始响应，未推进断点")
        normalized = []
        for fixture in fixtures:
            league = fixture.get("league") or {}
            if league.get("id") != league_id or league.get("name", "").upper() not in ("LPL", "LEAGUE OF LEGENDS PRO LEAGUE"):
                raise ValueError("来源赛事标识不是已确认的 LPL，停止导入；请核对 leagueId")
            if not fixture.get("id"):
                raise ValueError("赛程缺少来源 ID；未推进断点")
            normalized.append(dict(seriesId=f"pandascore:{fixture['id']}", sourceId=str(fixture["id"]),
                                   league="LPL", season=fixture.get("serie"), stage=fixture.get("tournament"),
                                   startTime=fixture.get("begin_at") or fixture.get("scheduled_at"),
                                   status=fixture.get("status"), teams=fixture.get("opponents", []),
                                   games=fixture.get("games", []), timelineStatus="not-acquired"))
        done = len(fixtures) < 100
        with wh.connect() as db:
            for fixture in normalized:
                db.execute("INSERT OR REPLACE INTO fixtures VALUES (?,?,?,?)", ("pandascore", fixture["sourceId"], dumps(fixture), ref))
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (checkpoint, str(1 if done else page + 1)))
        saved += len(normalized)
        if done:
            return dict(fixtures=saved, complete=True, nextPage=1)
        page += 1
    return dict(fixtures=saved, complete=False, nextPage=page)


def fixtures(wh):
    with wh.connect() as db:
        return [json.loads(r[0]) for r in db.execute("SELECT document FROM fixtures ORDER BY source_id DESC")]
