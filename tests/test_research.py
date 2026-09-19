import copy
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lolab import research, state, store, quality, discovery
from lolab.archive import Warehouse, dumps, safe_url
from lolab.transport import Client, DeferredRequest, retry_delay
from tests.fixtures import game


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.wh=Warehouse(self.root)
        self.m,self.t,self.c=game()
    def capture(self, obj, kind, source="test"):
        return self.wh.capture(dumps(obj).encode(),source=source,kind=kind,source_id=obj["metadata"]["matchId"],endpoint="https://example.org/match?api_key=SECRET",api_version="v1")
    def ingest(self):
        return research.import_game(self.wh,self.capture(self.m,"match"),self.capture(self.t,"timeline"),self.c)
    def codes(self):
        return {i["code"] for i in research.normalize(self.m,self.t,self.c)["quality"]["issues"]}
    def test_exact_raw_bytes_and_dedup(self):
        raw=b'{ "unrecognized" : [1,2], "nested":{"new":null}}\n'
        a=self.wh.capture(raw,source="test",kind="response",source_id="1",endpoint="url")
        b=self.wh.capture(raw,source="test",kind="response",source_id="1",endpoint="url")
        self.assertNotEqual(a,b);self.assertEqual(self.wh.raw(a),raw)
        self.assertEqual(self.wh.overview()["blobs"],1);self.assertEqual(self.wh.overview()["responses"],2)
    def test_immutable_history(self):
        self.ingest()
        for table in ("blobs","responses","revisions","mappings"):
            with self.assertRaises(sqlite3.IntegrityError),self.wh.connect() as db:
                db.execute(f"DELETE FROM {table}")
    def test_new_payload_keeps_both_revisions(self):
        first=self.capture(self.m,"match"); self.m["newField"]="preserve-me"
        second=self.capture(self.m,"match")
        self.assertNotEqual(self.wh.raw(first),self.wh.raw(second))
        self.assertNotIn("SECRET",self.wh.response(second)["endpoint"])
    def test_quality_good_synthetic(self):
        doc=self.ingest();self.assertTrue(doc["quality"]["ok"],doc["quality"])
        self.assertNotIn("blueWin",doc["meta"])
    def test_missing_zero_and_incomplete_aggregate(self):
        del self.t["info"]["frames"][1]["participantFrames"]["1"]["xp"]
        built=state.build(self.m,self.t)
        self.assertIsNone(built["frames"][1]["players"][0]["xp"])
        self.assertIsNone(built["frames"][1]["diff"]["xp"])
        self.assertEqual(built["frames"][0]["players"][0]["xp"],0)
        self.assertIn("missing_value",self.codes())
    def test_future_event_not_applied_early(self):
        self.t["info"]["frames"][1]["events"]=[dict(type="CHAMPION_KILL",timestamp=120000,killerId=1,victimId=6)]
        frames=state.build(self.m,self.t)["frames"]
        self.assertEqual(frames[1]["teams"]["100"]["kills"],0)
        self.assertEqual(frames[2]["teams"]["100"]["kills"],1)
        self.assertIn("event_time",self.codes())
    def test_duplicate_event_and_resources_separate(self):
        events=[dict(type="ELITE_MONSTER_KILL",timestamp=60000,killerId=1,killerTeamId=100,monsterType=m) for m in ("HORDE","RIFTHERALD","BARON_NASHOR","ATAKHAN")]
        self.t["info"]["frames"][1]["events"]=events+[events[0]]
        frame=state.build(self.m,self.t)["frames"][1]
        self.assertEqual([frame["teams"]["100"][k] for k in ("voidgrubs","heralds","barons","atakhans")],[1,1,1,1])
        self.assertIn("duplicate_event",self.codes())
    def test_missing_events_and_gaps_unknown_cumulative(self):
        self.t["info"]["frames"].pop(1)
        built=state.build(self.m,self.t)
        self.assertIsNone(built["frames"][1]["teams"]["100"]["kills"])
        self.assertIsNone(built["frames"][1]["players"][0]["items"])
        self.assertIn("frame_gap",self.codes())
    def test_missing_event_array_not_empty(self):
        del self.t["info"]["frames"][1]["events"]
        self.assertIn("missing_events",self.codes())
        self.assertIsNone(state.build(self.m,self.t)["frames"][1]["diff"]["kills"])
    def test_missing_win_not_false(self):
        del self.m["info"]["teams"][0]["win"]
        self.assertIsNone(state.build(self.m,self.t)["meta"]["blueWin"])
        self.assertIn("winner",self.codes())
    def test_identity_not_list_order(self):
        self.m["info"]["participants"].reverse()
        self.t["info"]["participants"]=[]
        self.assertEqual(state.build(self.m,self.t)["participants"][0]["puuid"],"synthetic-player-1")
    def test_inconsistent_gold_and_score(self):
        self.t["info"]["frames"][0]["participantFrames"]["1"]["currentGold"]=1000
        self.m["info"]["teams"][0]["objectives"]={"champion":{"kills":4}}
        self.assertTrue({"gold_inconsistent","final_mismatch"}.issubset(self.codes()))
    def test_empty_and_truncated_timeline(self):
        self.t["info"]["frames"]=[]
        self.assertIn("empty_timeline",self.codes())
    def test_unknown_resource_blocks(self):
        self.t["info"]["frames"][1]["events"]=[dict(type="ELITE_MONSTER_KILL",timestamp=60000,killerId=1,monsterType="FUTURE_MONSTER")]
        self.assertIn("unknown_mechanic",self.codes())
    def test_nonfinite_and_boolean_unknown(self):
        for v in (float("nan"),float("inf"),False,"123"):
            self.assertIsNone(state.number(v))
    def test_rebuild_offline_idempotent(self):
        doc=self.ingest()
        with patch("urllib.request.urlopen",side_effect=AssertionError("network forbidden")):
            rebuilt=research.rebuild(self.wh)[0]
        self.assertEqual(doc,rebuilt);self.assertEqual(self.wh.overview()["revisions"],1)
    def test_correction_audit_preserves_raw(self):
        doc=self.ingest();ref=doc["provenance"]["timelineResponse"];before=self.wh.raw(ref)
        path="/timeline/info/frames/1/participantFrames/1/currentGold"
        updated=research.correct(self.wh,doc["gameId"],dict(path=path,expected=500,value=600),"reviewer","checked source")
        self.assertEqual(updated["frames"][1]["players"][0]["currentGold"],600)
        self.assertEqual(self.wh.raw(ref),before);self.assertEqual(len(self.wh.corrections(doc["gameId"])),1)
        self.assertEqual(research.rebuild(self.wh)[0]["frames"][1]["players"][0]["currentGold"],600)
        with self.assertRaises(ValueError):
            research.correct(self.wh,doc["gameId"],dict(path=path,expected=500,value=700),"reviewer","stale")
    def test_correction_requires_reason_and_allowed_path(self):
        doc=self.ingest()
        for p,author,reason in [(dict(path="/labels/blueWin",expected=True,value=False),"a","reason"),(dict(path="/match/info/gameVersion",expected="test.1.1",value="test.2.1"),"a","")]:
            with self.assertRaises(ValueError):research.correct(self.wh,doc["gameId"],p,author,reason)
    def test_mismatched_ids_rejected(self):
        self.t["metadata"]["matchId"]="different"
        with self.assertRaises(ValueError):self.ingest()
    def test_mapping_conflict_rejected(self):
        self.wh.add_mapping("source","id","game1","evidence")
        with self.assertRaises(ValueError):self.wh.add_mapping("source","id","game2","evidence")
    def test_synthetic_never_passes_ten_game_gate(self):
        for i in range(10):
            self.m,self.t,self.c=game(f"TEST_{i}");self.ingest()
        report=research.validate_lpl(self.wh)
        self.assertEqual(report["candidateGames"],10);self.assertEqual(report["qualifiedGames"],0)
        self.assertEqual(report["status"],"blocked")
        self.assertEqual(research.analytics(self.wh)["included"],0)
    def test_empty_analytics_no_made_up_probability(self):
        self.assertEqual(research.analytics(self.wh)["groups"],[])
    def test_descriptive_stats_sample_count_and_patch_isolation(self):
        self.c["synthetic"]=False # Exercise statistics branch; fixture remains synthetic test code.
        self.ingest();stats=research.analytics(self.wh)
        self.assertEqual(stats["included"],1)
        self.assertEqual(stats["groups"][0]["conversions"][1]["games"],1)
        self.assertEqual(stats["groups"][0]["conversions"][1]["rate"],1)
    def test_feature_whitelist_and_backward_snapshot(self):
        doc=self.ingest();f=research.features_at(doc,905000)
        self.assertEqual(f["timestampMs"],900000)
        self.assertNotIn("blueWin",dumps(f));self.assertNotIn("winner",dumps(f))
        self.assertIsNone(research.features_at(doc,-1))
        self.assertIsNone(research.features_at(doc,1900000))
    def test_source_start_time_used_for_temporal_split(self):
        self.m["info"]["gameStartTimestamp"]=1767225600000
        del self.c["startTime"]
        doc=self.ingest()
        self.assertEqual(doc["context"]["startTime"],"2026-01-01T00:00:00+00:00")

    def test_series_temporal_boundary(self):
        a=self.ingest();b=copy.deepcopy(a);b["gameId"]="second";b["context"]["startTime"]="2026-01-02T00:00:00Z"
        split=research.temporal_split([a,b],"2026-01-01T12:00:00Z")
        self.assertEqual(split["train"],[]);self.assertEqual(len(split["test"]),2)
    def test_legacy_archive_compatibility_and_path_safety(self):
        store.save(self.root,"match",self.m["metadata"]["matchId"],self.m)
        store.save(self.root,"timeline",self.m["metadata"]["matchId"],self.t)
        self.assertEqual(store.inventory(self.root)["total"],1)
        self.assertEqual(store.locate(self.root,self.m["metadata"]["matchId"])["timeline"][0].data,self.t)
        with self.assertRaises(ValueError):store.save(self.root,"match","../../escape",self.m)

    def test_latest_invalid_raw_does_not_replace_last_valid(self):
        good=self.capture(self.m,"match", "riot-match-v5")
        self.wh.capture(b"invalid json",source="riot-match-v5",kind="match",source_id=self.m["metadata"]["matchId"],endpoint="test")
        self.assertEqual(self.wh.latest_response(self.m["metadata"]["matchId"],"match")["id"],good)
    def test_finished_game_is_not_prediction_sample(self):
        self.assertIsNone(research.features_at(self.ingest(),1800000))
    def test_migration_preserves_exact_archived_response_reference(self):
        a=self.capture(self.m,"match", "riot-match-v5"); b=self.capture(self.t,"timeline", "riot-match-v5")
        result=research.migrate_legacy(self.wh,self.root)
        self.assertEqual(len(result["imported"]),1)
        self.assertEqual(self.wh.game(result["imported"][0])["provenance"]["matchResponse"],a)
        self.assertEqual(self.wh.game(result["imported"][0])["provenance"]["timelineResponse"],b)
        self.assertEqual(self.wh.overview()["responses"],2)
    def test_permissions_gate_requires_explicit_approval(self):
        for i in range(10):
            self.m,self.t,self.c=game(f"GATE_{i}")
            self.c["synthetic"]=False # Exercise gate mechanics, not real-world evidence.
            self.t["info"]["frames"][0]["events"]=[dict(type="ITEM_PURCHASED",participantId=1,itemId=1001,timestamp=0)]
            self.ingest()
        blocked=research.validate_lpl(self.wh)
        self.assertEqual(blocked["qualifiedGames"],10)
        self.assertEqual(blocked["status"],"blocked")
        attestation={k:"synthetic branch test only" for k in ("reviewer","evidence","discoveryEvidence","historyRange","rateLimitEvidence")}
        attestation.update(sources=["test"],storagePermission={"status":"approved","evidence":"test"},modelUsePermission={"status":"denied","evidence":"test"})
        self.assertEqual(research.validate_lpl(self.wh,attestation)["status"],"blocked")
        attestation["modelUsePermission"]["status"]="approved"
        self.assertEqual(research.validate_lpl(self.wh,attestation)["status"],"passed-with-manual-attestation")
    def test_patch_mismatch_item_cost_not_used(self):
        from lolab import ddragon
        path=ddragon.cache_path(self.root);path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version":"new.1","costs":{"1001":500}}))
        self.assertEqual(ddragon.load(self.root,"old.1"),{})



class Response(io.BytesIO):
    status=200
    headers={"Content-Type":"application/json","Set-Cookie":"SECRET"}

class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.wh=Warehouse(Path(self.tmp.name))
    def test_success_resume_no_refetch(self):
        calls=[]
        def opener(*a,**k):calls.append(1);return Response(b'{ "new":1 }')
        client=Client(self.wh,"source","v1",interval=0,opener=opener)
        a=client.fetch("https://example.org/test","match","1");b=client.fetch("https://example.org/test","match","1")
        self.assertEqual(a,b);self.assertEqual(len(calls),1)
        self.assertNotIn("SECRET",self.wh.response(a[0])["headers"])
    def test_long_retry_after_persisted_not_capped(self):
        def opener(*a,**k):raise urllib.error.HTTPError("url",429,"rate",{"Retry-After":"3600"},io.BytesIO(b'limited'))
        client=Client(self.wh,"source","v1",interval=0,opener=opener)
        with self.assertRaises(DeferredRequest):client.fetch("https://example.org","match","1")
        self.assertEqual(self.wh.overview()["responses"],1)
        with self.assertRaises(DeferredRequest):client.fetch("https://example.org","match","1",refresh=True)
        self.assertEqual(self.wh.overview()["jobs"][0]["attempts"],1)
    def test_429_cooldown_applies_to_different_game_after_restart(self):
        def opener(*a,**k):raise urllib.error.HTTPError("url",429,"rate",{"Retry-After":"3600"},io.BytesIO(b'limited'))
        first=Client(self.wh,"source","v1",interval=0,opener=opener)
        with self.assertRaises(DeferredRequest):first.fetch("https://example.org/a","match","a")
        second=Client(Warehouse(self.wh.path.parent.parent),"source","v1",interval=0,opener=lambda *a,**k: self.fail("must respect source cooldown"))
        with self.assertRaises(DeferredRequest):second.fetch("https://example.org/b","match","b")

    def test_retry_503_then_success_archives_both(self):
        calls=[]
        def opener(*a,**k):
            calls.append(1)
            if len(calls)==1:raise urllib.error.HTTPError("url",503,"retry",{},io.BytesIO(b'upstream failure'))
            return Response(b'{}')
        with patch("lolab.transport.time.sleep"):
            Client(self.wh,"source","v1",interval=0,opener=opener).fetch("https://example.org","match","1")
        self.assertEqual(self.wh.overview()["responses"],2)
    def test_auth_error_not_retried(self):
        def opener(*a,**k):raise urllib.error.HTTPError("url",403,"auth",{},io.BytesIO(b'denied'))
        with self.assertRaises(ValueError):Client(self.wh,"source","v1",interval=0,opener=opener).fetch("https://example.org","match","1")
        self.assertEqual(self.wh.overview()["jobs"][0]["attempts"],1)
    def test_malformed_json_retained(self):
        with self.assertRaises(ValueError):Client(self.wh,"source","v1",interval=0,opener=lambda *a,**k:Response(b'broken')).fetch("https://example.org","match","1")
        self.assertEqual(self.wh.raw(1),b'broken')
    def test_retry_after_http_date(self):
        self.assertGreater(retry_delay("Wed, 01 Jan 2031 00:00:00 GMT",0),30)
        self.assertEqual(retry_delay("bad",3),8)
    def test_url_redaction(self):
        self.assertNotIn("SECRET",safe_url("https://user:SECRET@example.com/x?api_key=SECRET&foo=1"))
    def test_schedule_checkpoint_and_dedup(self):
        payload=[dict(id=1,league={"id":99,"name":"LPL"},games=[{"id":2}],opponents=[])]
        class Fake:
            def fetch(inner,*args,**kwargs):return 1,payload
        result=discovery.discover(self.wh,{"provider":"pandascore","leagueId":99},client=Fake())
        self.assertTrue(result["complete"])
        discovery.discover(self.wh,{"provider":"pandascore","leagueId":99},client=Fake())
        self.assertEqual(len(discovery.fixtures(self.wh)),1)
        self.assertEqual(discovery.fixtures(self.wh)[0]["timelineStatus"],"not-acquired")
    def test_wrong_league_not_accepted(self):
        class Fake:
            def fetch(inner,*args,**kwargs):return 1,[dict(id=1,league={"id":99,"name":"LCK"})]
        with self.assertRaises(ValueError):discovery.discover(self.wh,{"provider":"pandascore","leagueId":99},client=Fake())

if __name__ == "__main__":unittest.main()
