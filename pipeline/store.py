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
-- A fighter drafted before they had ever fought was carried under a temporary
-- id. When the real record turns up, the old id has to keep resolving or the
-- manager holding it silently loses a fighter.
CREATE TABLE IF NOT EXISTS aliases (
  from_id TEXT PRIMARY KEY,
  to_id   TEXT NOT NULL,
  at      REAL
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


# --------------------------------------------------------------------------
# Folding two copies of the database together
#
# Two machines write this file. GitHub writes it on a schedule; the PC writes
# it whenever somebody runs catch-up.ps1 — which exists precisely because
# ufcstats will talk to a home connection on days it refuses a datacenter. So
# the two copies diverge by design, and on any given day each usually holds
# something the other does not: the runner has a fresh rankings and roster
# pass, the PC has the card that actually got scraped.
#
# Picking a winner therefore throws away real work whichever way you pick, and
# git cannot help — to git this is a binary file two people rewrote, which is a
# conflict and nothing more. So merge it here, row by row, where the meaning of
# each row is known. Every rule below answers the same question: which of these
# two versions of this row was written by someone who knew more?

# A card that has finished outranks one that is still only announced, whatever
# the timestamps say: 'completed' is the state you can only reach by having
# read the result.
_STATUS_RANK = {"completed": 2, "scheduled": 1, "announced": 1}


def _rank(status: str | None) -> int:
    return _STATUS_RANK.get(status or "", 0)


def _stamp(row, field: str) -> float:
    try:
        return float(row[field] or 0)
    except (KeyError, IndexError, TypeError):
        return 0.0


def _wins(incoming, current, status_field: str | None, stamp_field: str) -> bool:
    """Is `incoming` the better-informed version of this row?"""
    if status_field:
        a, b = _rank(incoming[status_field]), _rank(current[status_field])
        if a != b:
            return a > b
    return _stamp(incoming, stamp_field) > _stamp(current, stamp_field)


def _stat_signal(row) -> int:
    """How much of a fight a statistics row appears to describe.

    Rows scraped before the totals-table fix hold round one's numbers rather
    than the whole fight's, so they are strictly smaller than a correct row for
    the same bout. Attempts rather than landed, because attempts scale with time
    in the cage and do not depend on how good the night was.
    """
    return sum(int(row[c] or 0) for c in
               ("sig_str_attempted", "total_str_attempted", "td_attempted", "ctrl_sec"))


def merge_from(con, other_path: Path | str) -> dict:
    """Fold another copy of this database into this one. Returns what moved.

    Never removes anything: a row present here and absent there stays. The
    other file is opened through connect(), so an older copy is brought up to
    the current schema first — which does write to it, and is why callers pass
    a temporary copy rather than the original.
    """
    other = connect(other_path)
    moved = dict.fromkeys(
        ("events", "bouts", "bout_stats", "fighters", "flags", "aliases", "runs"), 0)

    for row in other.execute("SELECT * FROM events"):
        cur = con.execute("SELECT * FROM events WHERE event_id=?",
                          (row["event_id"],)).fetchone()
        if cur is None or _wins(row, cur, "status", "scraped_at"):
            _upsert(con, "events", ["event_id"], dict(row))
            moved["events"] += 1

    for row in other.execute("SELECT * FROM bouts"):
        cur = con.execute("SELECT * FROM bouts WHERE bout_id=?",
                          (row["bout_id"],)).fetchone()
        if cur is None or _wins(row, cur, "status", "updated_at"):
            _upsert(con, "bouts", ["bout_id"], dict(row))
            moved["bouts"] += 1

    for row in other.execute("SELECT * FROM bout_stats"):
        cur = con.execute("SELECT * FROM bout_stats WHERE bout_id=? AND fighter_id=?",
                          (row["bout_id"], row["fighter_id"])).fetchone()
        if cur is None or _stat_signal(row) > _stat_signal(cur):
            _upsert(con, "bout_stats", ["bout_id", "fighter_id"], dict(row))
            moved["bout_stats"] += 1

    # A fighter is written by two different passes that know different things,
    # so the row is merged in two halves. Taking the whole row on updated_at
    # would let a fighter-page refresh silently undo a roster pass — every
    # released fighter quietly becomes signable again, with nothing in any log
    # to say why. That is the exact failure go-serverless.ps1 refuses to make,
    # and it must not happen here either.
    for row in other.execute("SELECT * FROM fighters"):
        cur = con.execute("SELECT * FROM fighters WHERE fighter_id=?",
                          (row["fighter_id"],)).fetchone()
        if cur is None:
            _upsert(con, "fighters", ["fighter_id"], dict(row))
            moved["fighters"] += 1
            continue
        keep = dict(cur)
        touched = False
        if _stamp(row, "updated_at") > _stamp(cur, "updated_at"):
            for c in ("name", "nickname", "wins", "losses", "draws",
                      "division", "rank", "updated_at"):
                keep[c] = row[c]
            touched = True
        if _stamp(row, "roster_at") > _stamp(cur, "roster_at"):
            keep["on_roster"], keep["roster_at"] = row["on_roster"], row["roster_at"]
            touched = True
        if touched:
            _upsert(con, "fighters", ["fighter_id"], keep)
            moved["fighters"] += 1

    for row in other.execute("SELECT * FROM flags"):
        cur = con.execute("SELECT 1 FROM flags WHERE bout_id=? AND fighter_id=? AND type=?",
                          (row["bout_id"], row["fighter_id"], row["type"])).fetchone()
        if cur is None:
            con.execute("INSERT OR IGNORE INTO flags (bout_id,fighter_id,type) VALUES (?,?,?)",
                        (row["bout_id"], row["fighter_id"], row["type"]))
            moved["flags"] += 1

    for row in other.execute("SELECT * FROM aliases"):
        cur = con.execute("SELECT * FROM aliases WHERE from_id=?",
                          (row["from_id"],)).fetchone()
        if cur is None or _stamp(row, "at") > _stamp(cur, "at"):
            _upsert(con, "aliases", ["from_id"], dict(row))
            moved["aliases"] += 1

    # The run log is diagnostics, but it is the diagnostics somebody reads at
    # 8am when the standings have not moved, so keep both machines' entries.
    # `id` is an autoincrement and means nothing across two files; (kind,
    # started_at) is what actually identifies a run.
    for row in other.execute("SELECT * FROM runs"):
        cur = con.execute("SELECT 1 FROM runs WHERE kind=? AND started_at=?",
                          (row["kind"], row["started_at"])).fetchone()
        if cur is None:
            con.execute("INSERT INTO runs (kind,started_at,finished_at,ok,detail)"
                        " VALUES (?,?,?,?,?)",
                        (row["kind"], row["started_at"], row["finished_at"],
                         row["ok"], row["detail"]))
            moved["runs"] += 1

    con.commit()
    other.close()
    log.info("merged in: %s", ", ".join(f"{v} {k}" for k, v in moved.items() if v) or "nothing")
    return moved


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
