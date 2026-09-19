"""Append-only, compressed response archive and rebuildable research catalog.

SQLite triggers protect history from accidental UPDATE/DELETE, not a disk owner.
Back up the whole database with SQLite's backup API; never copy an active WAL alone.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def safe_url(url):
    parts = urlsplit(url)
    if not parts.scheme:
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    query = [(k, "[REDACTED]" if any(s in k.lower() for s in ("key", "token", "secret", "signature", "auth")) else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))


class Warehouse:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "research" / "warehouse.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS responses (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
                    source_id TEXT NOT NULL, endpoint TEXT NOT NULL, requested_at TEXT NOT NULL,
                    api_version TEXT NOT NULL, status INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    headers TEXT NOT NULL, fidelity TEXT NOT NULL,
                    FOREIGN KEY(sha256) REFERENCES blobs(sha256));
                CREATE INDEX IF NOT EXISTS response_lookup ON responses(source_id, kind, id);
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    input_hash TEXT NOT NULL, document TEXT NOT NULL,
                    UNIQUE(game_id,input_hash));
                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    author TEXT NOT NULL, reason TEXT NOT NULL, patch TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audits (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mappings (
                    source TEXT NOT NULL, source_id TEXT NOT NULL, game_id TEXT NOT NULL,
                    evidence TEXT NOT NULL, PRIMARY KEY(source, source_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    job_key TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
                    error TEXT, response_id INTEGER);
                CREATE TABLE IF NOT EXISTS fixtures (
                    source TEXT NOT NULL, source_id TEXT NOT NULL, document TEXT NOT NULL,
                    response_id INTEGER NOT NULL, PRIMARY KEY(source,source_id));
                CREATE TABLE IF NOT EXISTS source_limits (source TEXT PRIMARY KEY, next_request REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            for table in ("blobs", "responses", "revisions", "corrections", "audits", "mappings"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only history'); END''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, body: bytes, *, source, kind, source_id, endpoint,
                api_version="unknown", status=200, headers=None, requested_at=None,
                fidelity="response-bytes"):
        sha = digest(body)
        # Never persist request headers/cookies/credentials. Only harmless response metadata.
        allowed = {"content-type", "etag", "last-modified", "retry-after", "x-rate-limit-type"}
        clean_headers = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in allowed}
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (sha, gzip.compress(body, mtime=0), len(body)))
            cursor = db.execute('''INSERT INTO responses
                (source,kind,source_id,endpoint,requested_at,api_version,status,sha256,headers,fidelity)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (source, kind, source_id, safe_url(endpoint), requested_at or now(), api_version,
                 status, sha, dumps(clean_headers), fidelity))
            return cursor.lastrowid

    def raw(self, response_id):
        with self.connect() as db:
            row = db.execute("SELECT b.* FROM blobs b JOIN responses r ON r.sha256=b.sha256 WHERE r.id=?", (response_id,)).fetchone()
        if row is None:
            raise ValueError("原始响应不存在")
        body = gzip.decompress(row["body"])
        if digest(body) != row["sha256"]:
            raise ValueError("原始数据哈希校验失败")
        return body

    def response(self, response_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM responses WHERE id=?", (response_id,)).fetchone()
        return dict(row) if row else None

    def latest_response(self, source_id, kind, source=None):
        with self.connect() as db:
            rows = db.execute('''SELECT * FROM responses WHERE source_id=? AND kind=?
                AND status=200 AND (? IS NULL OR source=?) ORDER BY id DESC''',
                (source_id, kind, source, source)).fetchall()
        for row in rows:
            if kind in ("match", "timeline"):
                try:
                    obj = json.loads(self.raw(row["id"]))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("info"), dict):
                    continue
                if (obj.get("metadata") or {}).get("matchId") != source_id:
                    continue
                field = "participants" if kind == "match" else "frames"
                if not isinstance(obj["info"].get(field), list):
                    continue
            return dict(row)
        return None

    def add_mapping(self, source, source_id, game_id, evidence):
        with self.connect() as db:
            row = db.execute("SELECT game_id FROM mappings WHERE source=? AND source_id=?", (source, source_id)).fetchone()
            if row and row[0] != game_id:
                raise ValueError(f"来源 ID 对照冲突：{source}:{source_id}")
            db.execute("INSERT OR IGNORE INTO mappings VALUES (?,?,?,?)", (source, source_id, game_id, evidence))

    def save_revision(self, game_id, input_hash, document):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO revisions(game_id,created_at,input_hash,document) VALUES (?,?,?,?)",
                       (game_id, now(), input_hash, dumps(document)))
        return document

    def games(self):
        with self.connect() as db:
            rows = db.execute('''SELECT document FROM revisions WHERE id IN
                (SELECT MAX(id) FROM revisions GROUP BY game_id) ORDER BY id DESC''').fetchall()
        return [json.loads(r[0]) for r in rows]

    def game(self, game_id):
        with self.connect() as db:
            row = db.execute("SELECT document FROM revisions WHERE game_id=? ORDER BY id DESC LIMIT 1", (game_id,)).fetchone()
        if not row:
            raise ValueError("比赛尚未导入研究仓库")
        return json.loads(row[0])

    def corrections(self, game_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM corrections WHERE game_id=? ORDER BY id", (game_id,)).fetchall()
        return [{**dict(r), "patch": json.loads(r["patch"])} for r in rows]

    def overview(self):
        with self.connect() as db:
            counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("responses", "blobs", "revisions", "corrections", "fixtures")}
            counts["jobs"] = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 100")]
            audit = db.execute("SELECT document FROM audits ORDER BY id DESC LIMIT 1").fetchone()
            counts["audit"] = json.loads(audit[0]) if audit else None
        return counts
