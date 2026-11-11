"""SQLite storage for drives, SMART snapshots, events and scheduler state."""
import json
import os
import sqlite3
import threading
import time

from config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    serial      TEXT PRIMARY KEY,
    model       TEXT,
    device      TEXT,
    dev_type    TEXT,
    protocol    TEXT,
    first_seen  INTEGER,
    last_seen   INTEGER,
    state       TEXT DEFAULT 'online',   -- online | standby | missing
    summary     TEXT,                    -- normalized SMART data (JSON)
    analysis    TEXT,                    -- latest analysis (JSON)
    raw         TEXT                     -- latest raw smartctl JSON
);
CREATE TABLE IF NOT EXISTS snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    serial  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    metrics TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(serial, ts);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    serial  TEXT,
    level   TEXT NOT NULL,
    title   TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS benchmarks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    serial  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    result  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench ON benchmarks(serial, ts);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class DB:
    def __init__(self, path=None):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = path or os.path.join(DATA_DIR, "drivesentinel.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    # ---- drives -------------------------------------------------------
    def get_drive(self, serial):
        row = self._one("SELECT * FROM drives WHERE serial=?", (serial,))
        return _decode_drive(row)

    def list_drives(self):
        return [_decode_drive(r) for r in self._all("SELECT * FROM drives ORDER BY device")]

    def upsert_drive(self, serial, summary, analysis, raw, ts):
        exists = self._one("SELECT serial FROM drives WHERE serial=?", (serial,))
        args = (summary.get("model"), summary.get("device"), summary.get("dev_type"),
                summary.get("protocol"), ts, json.dumps(summary), json.dumps(analysis),
                json.dumps(raw))
        if exists:
            self._exec("""UPDATE drives SET model=?, device=?, dev_type=?, protocol=?,
                          last_seen=?, state='online', summary=?, analysis=?, raw=?
                          WHERE serial=?""", args + (serial,))
        else:
            self._exec("""INSERT INTO drives (model, device, dev_type, protocol, last_seen,
                          summary, analysis, raw, serial, first_seen, state)
                          VALUES (?,?,?,?,?,?,?,?,?,?, 'online')""", args + (serial, ts))

    def set_drive_state(self, serial, state):
        self._exec("UPDATE drives SET state=? WHERE serial=?", (state, serial))

    def delete_drive(self, serial):
        self._exec("DELETE FROM drives WHERE serial=?", (serial,))
        self._exec("DELETE FROM snapshots WHERE serial=?", (serial,))
        self._exec("DELETE FROM benchmarks WHERE serial=?", (serial,))

    # ---- snapshots ----------------------------------------------------
    def add_snapshot(self, serial, ts, metrics):
        self._exec("INSERT INTO snapshots (serial, ts, metrics) VALUES (?,?,?)",
                   (serial, ts, json.dumps(metrics, separators=(",", ":"))))

    def history(self, serial, since_ts):
        rows = self._all("SELECT ts, metrics FROM snapshots WHERE serial=? AND ts>=? ORDER BY ts",
                         (serial, since_ts))
        return [(r["ts"], json.loads(r["metrics"])) for r in rows]

    def last_snapshot(self, serial):
        r = self._one("SELECT ts, metrics FROM snapshots WHERE serial=? ORDER BY ts DESC LIMIT 1",
                      (serial,))
        return (r["ts"], json.loads(r["metrics"])) if r else None

    def prune(self, retention_days):
        cutoff = int(time.time()) - retention_days * 86400
        self._exec("DELETE FROM snapshots WHERE ts<?", (cutoff,))
        self._exec("DELETE FROM events WHERE ts<?", (cutoff,))

    # ---- benchmarks ---------------------------------------------------
    def add_benchmark(self, serial, ts, result):
        self._exec("INSERT INTO benchmarks (serial, ts, result) VALUES (?,?,?)",
                   (serial, ts, json.dumps(result)))

    def benchmarks(self, serial, limit=200):
        rows = self._all("SELECT ts, result FROM benchmarks WHERE serial=? ORDER BY ts DESC LIMIT ?",
                         (serial, limit))
        return [dict(json.loads(r["result"]), ts=r["ts"]) for r in reversed(rows)]

    # ---- events -------------------------------------------------------
    def add_event(self, level, title, message="", serial=None, ts=None):
        self._exec("INSERT INTO events (ts, serial, level, title, message) VALUES (?,?,?,?,?)",
                   (ts or int(time.time()), serial, level, title, message))

    def events(self, limit=200, serial=None):
        if serial:
            return self._all("SELECT * FROM events WHERE serial=? ORDER BY ts DESC, id DESC LIMIT ?",
                             (serial, limit))
        return self._all("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    # ---- key/value state ---------------------------------------------
    def kv_get(self, key, default=None):
        r = self._one("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, key, value):
        self._exec("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (key, json.dumps(value)))


def _decode_drive(row):
    if not row:
        return None
    for key in ("summary", "analysis", "raw"):
        row[key] = json.loads(row[key]) if row.get(key) else None
    return row
