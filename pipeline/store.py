"""SQLite system of record.

The database — not the JSON export, and not the app — is the source of truth.
Every write is an idempotent upsert keyed on the source's own stable id, so a
run that crashes halfway can simply be run again. Re-scraping an event that has
not changed is a no-op.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import config

log = logging.getLogger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fighters (
  fighter_id TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  nickname   TEXT DEFAULT '',
  wins       INTEGER DEFAULT 0,
  losses     INTEGER DEFAULT 0,
  draws      INTEGER DEFAULT 0,
  division   TEXT DEFAULT '',
  rank       INTEGER,                     -- 0 = champion, NULL = unranked
  on_roster  INTEGER,                     -- 1 under contract, 0 released, NULL unknown
  roster_at  REAL,                        -- when the roster list last confirmed them
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  event_id   TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  date       TEXT,
  location   TEXT DEFAULT '',
  status     TEXT DEFAULT 'scheduled',    -- scheduled | completed
  scraped_at REAL
);
CREATE TABLE IF NOT EXISTS bouts (
  bout_id     TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL,
  fighter_a   TEXT,
  fighter_b   TEXT,
  weight_class TEXT DEFAULT '',
  title_bout  INTEGER DEFAULT 0,
  card_position INTEGER DEFAULT 0,        -- 0 = main event (ufcstats lists top-down)
  status      TEXT DEFAULT 'announced',   -- announced | completed
  outcome     TEXT DEFAULT '',            -- win | draw | nc
  winner_id   TEXT,
  method      TEXT DEFAULT '',
  method_detail TEXT DEFAULT '',
  round       INTEGER DEFAULT 0,
  time        TEXT DEFAULT '',
  bonuses     TEXT DEFAULT '[]',
  updated_at  REAL,
  FOREIGN KEY (event_id) REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS bout_stats (
  bout_id    TEXT NOT NULL,
  fighter_id TEXT NOT NULL,
  kd INTEGER DEFAULT 0,
  sig_str_landed INTEGER DEFAULT 0,
  sig_str_attempted INTEGER DEFAULT 0,
  total_str_attempted INTEGER DEFAULT 0,
  td_landed INTEGER DEFAULT 0,
  td_attempted INTEGER DEFAULT 0,
  sub_att INTEGER DEFAULT 0,
  rev INTEGER DEFAULT 0,
  ctrl_sec INTEGER DEFAULT 0,
  PRIMARY KEY (bout_id, fighter_id)
);
CREATE TABLE IF NOT EXISTS flags (
  bout_id    TEXT NOT NULL,
  fighter_id TEXT NOT NULL,
  type       TEXT NOT NULL,               -- missed_weight | withdrew
  PRIMARY KEY (bout_id, fighter_id, type)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, started_at REAL, finished_at REAL, ok INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_bouts_event ON bouts(event_id);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _add_missing_columns(con)
    return con


# Databases built before a column existed are the normal case, not the
# exception — this project's whole point is a database that keeps running. New
# columns are added here rather than by asking anyone to rebuild.
_LATER_COLUMNS = {
    "fighters": [("on_roster", "INTEGER"), ("roster_at", "REAL")],
}


def _add_missing_columns(con) -> None:
    for table, columns in _LATER_COLUMNS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("added %s.%s", table, name)
    con.commit()


def _upsert(con, table: str, key: list[str], row: dict) -> None:
    cols = list(row)
    updates = [c for c in cols if c not in key]
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
           f"ON CONFLICT({','.join(key)}) DO UPDATE SET "
           + ",".join(f"{c}=excluded.{c}" for c in updates))
    con.execute(sql, [row[c] for c in cols])


def upsert_event(con, ev: dict) -> None:
    _upsert(con, "events", ["event_id"], {
        "event_id": ev["event_id"], "name": ev["name"], "date": ev.get("date"),
        "location": ev.get("location", ""), "status": ev.get("status", "scheduled"),
        "scraped_at": time.time(),
    })


def upsert_bout(con, event_id: str, b: dict, position: int = 0) -> None:
    ids = [f.get("fighter_id") for f in b.get("fighters", [])] + [None, None]
    _upsert(con, "bouts", ["bout_id"], {
        "bout_id": b["bout_id"], "event_id": event_id,
        "fighter_a": ids[0], "fighter_b": ids[1],
        "weight_class": b.get("weight_class", ""),
        "title_bout": int(bool(b.get("title_bout"))),
        "card_position": position,
        "status": b.get("status", "completed" if b.get("outcome") else "announced"),
        "outcome": b.get("outcome", ""), "winner_id": b.get("winner_id"),
        "method": b.get("method", ""), "method_detail": b.get("method_detail", ""),
        "round": b.get("round", 0), "time": b.get("time", ""),
        "bonuses": json.dumps(b.get("bonuses", [])),
        "updated_at": time.time(),
    })
    for fid, st in (b.get("stats") or {}).items():
        _upsert(con, "bout_stats", ["bout_id", "fighter_id"],
                {"bout_id": b["bout_id"], "fighter_id": fid, **st})


def upsert_fighter(con, f: dict) -> None:
    row = {"fighter_id": f["fighter_id"], "name": f["name"],
           "nickname": f.get("nickname", ""), "wins": f.get("wins", 0),
           "losses": f.get("losses", 0), "draws": f.get("draws", 0),
           "updated_at": time.time()}
    if f.get("division") is not None:
        row["division"] = f.get("division", "")
    if "rank" in f:
        row["rank"] = f["rank"]
    _upsert(con, "fighters", ["fighter_id"], row)


def set_flag(con, bout_id: str, fighter_id: str, type_: str) -> None:
    con.execute("INSERT OR IGNORE INTO flags (bout_id, fighter_id, type) VALUES (?,?,?)",
                (bout_id, fighter_id, type_))


def set_rank(con, fighter_id: str, division: str, rank: int | None) -> None:
    con.execute("UPDATE fighters SET division=?, rank=?, updated_at=? WHERE fighter_id=?",
                (division, rank, time.time(), fighter_id))


def known_complete_events(con) -> set[str]:
    return {r["event_id"] for r in
            con.execute("SELECT event_id FROM events WHERE status='completed'")}


def events_needing_results(con) -> list[sqlite3.Row]:
    """Past-dated events we have not marked completed, or completed events with
    bouts that never got a result — the two ways a card silently goes unscored."""
    return list(con.execute("""
        SELECT DISTINCT e.* FROM events e
        LEFT JOIN bouts b ON b.event_id = e.event_id
        WHERE e.date <= date('now')
          AND (e.status != 'completed' OR b.bout_id IS NULL
               OR (b.status = 'announced'))
        ORDER BY e.date DESC
    """))


def log_run(con, kind: str, started: float, ok: bool, detail: str) -> None:
    con.execute("INSERT INTO runs (kind, started_at, finished_at, ok, detail) VALUES (?,?,?,?,?)",
                (kind, started, time.time(), int(ok), detail[:4000]))
    con.commit()


def stats(con) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "fighters": q("SELECT COUNT(*) FROM fighters"),
        "events": q("SELECT COUNT(*) FROM events"),
        "completed_events": q("SELECT COUNT(*) FROM events WHERE status='completed'"),
        "bouts": q("SELECT COUNT(*) FROM bouts"),
        "scored_bouts": q("SELECT COUNT(*) FROM bouts WHERE status='completed'"),
        "bouts_with_stats": q("SELECT COUNT(DISTINCT bout_id) FROM bout_stats"),
    }
