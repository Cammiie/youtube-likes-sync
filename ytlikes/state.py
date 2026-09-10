from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from .common import SyncError


class State:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen (video_id TEXT PRIMARY KEY, first_seen REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            video_id TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            candidate TEXT, catalog_id TEXT, path TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_try REAL NOT NULL DEFAULT 0, error TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS downloads (catalog_id TEXT PRIMARY KEY, path TEXT NOT NULL,
            completed REAL NOT NULL);
        """)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def baseline(self, songs: list[dict], account: str):
        if self.get("baseline_at") is not None:
            if self.get("account") != account:
                raise SyncError("different_youtube_account")
            return False
        now = time.time()
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)",
                                [(s["video_id"], now) for s in songs])
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                [("baseline_at", json.dumps(now)), ("account", json.dumps(account))])
        return True

    def unseen(self, songs):
        ids = {row[0] for row in self.db.execute("SELECT video_id FROM seen")}
        return [s for s in songs if s["video_id"] not in ids]

    def ingest(self, songs):
        if self.get("baseline_at") is None:
            raise SyncError("setup_required")
        fresh = self.unseen(songs)
        now = time.time()
        with self.db:
            for s in fresh:
                cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (s["video_id"], now))
                if cur.rowcount:
                    self.db.execute("INSERT INTO jobs(video_id,source,updated) VALUES (?,?,?)",
                                    (s["video_id"], json.dumps(s), now))
        return len(fresh)

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='pending',error='interrupted',next_try=0 WHERE state='downloading'")

    def due(self, limit=25):
        return self.db.execute("SELECT * FROM jobs WHERE state='pending' AND next_try<=? ORDER BY updated LIMIT ?",
                               (time.time(), limit)).fetchall()

    def selected(self, video_id, candidate, path):
        with self.db:
            self.db.execute("""UPDATE jobs SET candidate=?,catalog_id=?,path=?,state='downloading',updated=?
                               WHERE video_id=?""", (json.dumps(candidate), str(candidate["id"]), str(path),
                                                     time.time(), video_id))

    def completed(self, video_id, catalog_id, path):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?)", (str(catalog_id), str(path), time.time()))
            self.db.execute("UPDATE jobs SET state='completed',path=?,error=NULL,updated=? WHERE video_id=?",
                            (str(path), time.time(), video_id))

    def downloaded(self, catalog_id):
        row = self.db.execute("SELECT path FROM downloads WHERE catalog_id=?", (str(catalog_id),)).fetchone()
        return Path(row[0]) if row else None

    def fail(self, video_id, error: SyncError):
        row = self.db.execute("SELECT attempts FROM jobs WHERE video_id=?", (video_id,)).fetchone()
        attempts = row[0] + 1
        delay = max(error.delay, min(86400, 300 * 2 ** min(attempts - 1, 9)))
        with self.db:
            self.db.execute("""UPDATE jobs SET state='pending',attempts=?,next_try=?,error=?,updated=?
                               WHERE video_id=?""", (attempts, time.time() + delay, error.code, time.time(), video_id))

    def retry(self):
        with self.db:
            self.db.execute("UPDATE jobs SET next_try=0,attempts=0 WHERE state='pending'")

    def status(self):
        counts = {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")}
        jobs = []
        for r in self.db.execute("SELECT * FROM jobs ORDER BY updated DESC LIMIT 100"):
            item = dict(r)
            item["source"] = json.loads(item["source"])
            item["candidate"] = json.loads(item["candidate"]) if item["candidate"] else None
            jobs.append(item)
        return {"baseline_at": self.get("baseline_at"), "baseline_and_seen": self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0],
                "paused": self.get("paused", False), "auth_required": self.get("auth_required", False),
                "last_check": self.get("last_check"), "last_error": self.get("last_error"),
                "counts": counts, "jobs": jobs}
