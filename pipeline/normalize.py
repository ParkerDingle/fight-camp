"""Cross-source identity, division inference, and the export the app eats.

The one genuinely hard problem in this pipeline is that Wikipedia writes
"Alexandre Pantoja" and ufcstats writes "Alexandre Pantoja", right up until the
day one of them writes "Alex Pantoja" or adds an accent. Fighter ids only exist
on ufcstats, so anything learned from Wikipedia has to be matched back by name.

The rule here: match confidently or not at all. A wrong match silently awards
points to the wrong manager's fighter, which is worse than a missing bonus.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import config

log = logging.getLogger("normalize")

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
_SUFFIX = re.compile(r"\b(jr|sr|iii|ii|iv)\b")


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s.lower())
    s = _SUFFIX.sub(" ", s)
    return _WS.sub(" ", s).strip()


class NameIndex:
    """Maps a display name from any source onto a ufcstats fighter id."""

    def __init__(self, con, *, scraped_only: bool = False):
        self.exact: dict[str, str] = {}
        self.collisions: set[str] = set()
        for row in con.execute("SELECT fighter_id, name, nickname FROM fighters"):
            if scraped_only and not _SCRAPED_ID.match(row["fighter_id"] or ""):
                continue
            for candidate in (row["name"], row["nickname"]):
                key = norm_name(candidate)
                if not key:
                    continue
                if key in self.exact and self.exact[key] != row["fighter_id"]:
                    self.collisions.add(key)
                else:
                    self.exact[key] = row["fighter_id"]
        for key in self.collisions:
            self.exact.pop(key, None)
        self.keys = list(self.exact)

    def match(self, name: str) -> str | None:
        key = norm_name(name)
        if not key:
            return None
        if key in self.exact:
            return self.exact[key]
        close = difflib.get_close_matches(key, self.keys, n=2, cutoff=0.88)
        if len(close) == 1:
            log.debug("fuzzy matched %r -> %r", name, close[0])
            return self.exact[close[0]]
        if close:
            log.warning("ambiguous name %r (%s) — skipped", name, close)
        return None


def infer_divisions(con) -> int:
    """Fill in a division for fighters who do not have a ranked one.

    ufcstats has no division field, so we take the weight class of the most
    recent bout. But a *ranked* fighter's division is already known and more
    authoritative: a fighter who has moved up is ranked in the new division
    while their last bout was in the old one, and overwriting that puts two
    #1s in the same weight class on the draft board.
    """
    changed = 0
    rows = con.execute("""
        SELECT f.fighter_id, (
          SELECT b.weight_class FROM bouts b
          JOIN events e ON e.event_id = b.event_id
          WHERE (b.fighter_a = f.fighter_id OR b.fighter_b = f.fighter_id)
            AND b.weight_class IN ({placeholders})
          ORDER BY e.date DESC LIMIT 1
        ) AS division
        FROM fighters f
        WHERE f.rank IS NULL
    """.format(placeholders=",".join("?" * len(config.SCORABLE_DIVISIONS))),
        config.SCORABLE_DIVISIONS).fetchall()
    for r in rows:
        if r["division"]:
            con.execute("UPDATE fighters SET division=? WHERE fighter_id=? AND division!=?",
                        (r["division"], r["fighter_id"], r["division"]))
            changed += con.total_changes and 1 or 0
    con.commit()
    return changed


def apply_rankings(con, rankings: dict[str, list[str]]) -> int:
    """Rankings are cosmetic (draft board ordering) — never scoring input."""
    index = NameIndex(con)
    con.execute("UPDATE fighters SET rank=NULL")
    applied, seen = 0, set()
    for division, names in rankings.items():
        for position, name in enumerate(names):
            fid = index.match(name)
            if not fid:
                log.info("unmatched ranked fighter %r (%s)", name, division)
                continue
            if fid in seen:
                # Fighters who moved up appear in two divisions' tables. Keep
                # the first, or the board shows two #1s in one weight class.
                log.debug("%s already ranked; ignoring %s listing", name, division)
                continue
            seen.add(fid)
            con.execute("UPDATE fighters SET rank=?, division=? WHERE fighter_id=?",
                        (position, division, fid))
            applied += 1
    con.commit()

    # Two fighters holding the same slot in one division is impossible in real
    # rankings, so if it happens the parse is wrong. Say so — a draft board with
    # two #1 welterweights is the kind of thing everyone notices and nobody
    # reports.
    for row in con.execute("""
            SELECT division, rank, GROUP_CONCAT(name, ' / ') names, COUNT(*) n
            FROM fighters WHERE rank IS NOT NULL
            GROUP BY division, rank HAVING n > 1"""):
        log.warning("rankings clash: %s #%s held by %s",
                    row["division"], row["rank"], row["names"])
    return applied


# ufcstats ids are 16 hex characters. Anything else in the fighters table was
# put there by the roster pass for someone who has not fought in the UFC yet,
# and can be thrown away again the moment the real record shows up.
_SCRAPED_ID = re.compile(r"^[0-9a-f]{16}$")


def _roster_id(name: str) -> str:
    import hashlib
    return "w" + hashlib.sha1(norm_name(name).encode()).hexdigest()[:15]


def apply_roster(con, roster: dict[str, list[str]]) -> dict:
    """Mark who is currently under contract, and add anyone we have never seen.

    ufcstats only knows about people who have fought. A fighter signed last
    month is invisible to it, and one released yesterday looks identical to one
    between camps — so without this pass the draft pool is "whoever competed
    recently", which is neither the roster nor a fair board.

    Fighters with no scraped record get a placeholder row so they can be
    drafted; the moment they actually fight, the real record arrives and the
    placeholder is dropped.
    """
    if not roster:
        return {"matched": 0, "added": 0, "released": 0, "unmatched": []}

    index = NameIndex(con, scraped_only=True)
    con.execute("UPDATE fighters SET on_roster=0")
    now = time.time()
    matched, added, unmatched = 0, 0, []

    for division, names in roster.items():
        for name in names:
            fid = index.match(name)
            if fid:
                con.execute(
                    "UPDATE fighters SET on_roster=1, roster_at=?, "
                    "division=CASE WHEN division IS NULL OR division='' THEN ? "
                    "ELSE division END WHERE fighter_id=?",
                    (now, division, fid))
                matched += 1
                # They may have been drafted last month under a temporary id,
                # before they had ever fought. Leave a forwarding address so
                # whoever holds that id keeps the fighter.
                old = _roster_id(name)
                if old != fid and con.execute(
                        "SELECT 1 FROM fighters WHERE fighter_id=?", (old,)).fetchone():
                    con.execute("INSERT OR REPLACE INTO aliases (from_id, to_id, at) "
                                "VALUES (?,?,?)", (old, fid, now))
                    log.info("%s now has a real record; %s -> %s", name, old, fid)
                continue
            unmatched.append(f"{name} ({division})")
            con.execute(
                "INSERT INTO fighters (fighter_id, name, division, on_roster, "
                "roster_at, updated_at) VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(fighter_id) DO UPDATE SET on_roster=1, roster_at=excluded.roster_at",
                (_roster_id(name), name, division, now, now))
            added += 1

    # A roster is mostly people who have fought, so most names should land on a
    # scraped record. If most of them do not, the name column moved or the
    # matcher broke, and applying this would bury the draft pool under hundreds
    # of duplicate placeholders. Leave last week's roster alone instead.
    # A quarter of the roster failing to match is normal, not broken: plenty of
    # contracted fighters have not fought inside the scraped window, and they
    # are precisely who this pass exists to find. Half is not normal.
    total = matched + added
    if total >= 50 and added > total * 0.45:
        con.rollback()
        log.error("roster: %s of %s names matched nothing — refusing to apply. "
                  "First few: %s", added, total, ", ".join(unmatched[:5]))
        return {"matched": 0, "added": 0, "released": 0, "unmatched": unmatched,
                "rejected": True}

    # Placeholders that the list no longer carries, and that never fought, are
    # simply gone: released before debuting, or now matched to a real record.
    fought = {r[0] for r in con.execute(
        "SELECT fighter_a FROM bouts UNION SELECT fighter_b FROM bouts")}
    stale = [r["fighter_id"] for r in
             con.execute("SELECT fighter_id FROM fighters WHERE on_roster=0")
             if not _SCRAPED_ID.match(r["fighter_id"] or "")
             and r["fighter_id"] not in fought]
    con.executemany("DELETE FROM fighters WHERE fighter_id=?", [(f,) for f in stale])
    released = len(stale)
    con.commit()

    log.info("roster: %s matched, %s added as new signings, %s placeholders dropped",
             matched, added, released)
    if len(unmatched) > 80:
        log.warning("roster: %s names did not match a scraped fighter — that is a lot; "
                    "check the parse before trusting the pool", len(unmatched))
    return {"matched": matched, "added": added, "released": released,
            "unmatched": unmatched}


def apply_wiki_notes(con, event_id: str, card: dict) -> int:
    """Missed-weight flags and bonus awards learned from a Wikipedia article."""
    index = NameIndex(con)
    bouts = list(con.execute(
        "SELECT bout_id, fighter_a, fighter_b, bonuses FROM bouts WHERE event_id=?",
        (event_id,)))
    applied = 0

    for note in card.get("weigh_in_notes", []):
        fid = index.match(note["fighter"])
        if not fid:
            continue
        for b in bouts:
            if fid in (b["fighter_a"], b["fighter_b"]):
                con.execute(
                    "INSERT OR IGNORE INTO flags (bout_id, fighter_id, type) VALUES (?,?,?)",
                    (b["bout_id"], fid, note["type"]))
                applied += 1

    for bonus in card.get("bonuses", []):
        fid = index.match(bonus["fighter"])
        if not fid:
            continue
        for b in bouts:
            if fid in (b["fighter_a"], b["fighter_b"]):
                current = set(json.loads(b["bonuses"] or "[]"))
                if bonus["type"] not in current:
                    current.add(bonus["type"])
                    con.execute("UPDATE bouts SET bonuses=? WHERE bout_id=?",
                                (json.dumps(sorted(current)), b["bout_id"]))
                    applied += 1
    con.commit()
    return applied


# --------------------------------------------------------------------- export
def export(con, path: Path | None = None, *, since: str | None = None) -> dict:
    """Write the JSON the app loads. Keys are short because this ships inside
    the page; the shape mirrors what the client already expects."""
    path = Path(path or config.EXPORT_PATH)
    where = "WHERE e.date >= ?" if since else ""
    args = [since] if since else []

    # Career records come from fighter pages, which are the most expensive and
    # least important thing we scrape — and the first casualty when the site
    # throttles us. Where one is missing, fall back to the record inside the
    # data we already have. A fighter shown as "3-1" is informative; one shown
    # as "0-0" looks like the app is broken.
    tally: dict[str, list[int]] = {}
    for b in con.execute("SELECT fighter_a, fighter_b, winner_id, outcome "
                         "FROM bouts WHERE status='completed'"):
        for fid in (b["fighter_a"], b["fighter_b"]):
            if not fid:
                continue
            rec = tally.setdefault(fid, [0, 0])
            if b["outcome"] == "win" and b["winner_id"]:
                rec[0 if b["winner_id"] == fid else 1] += 1

    # Has the roster pass ever run? Before it has, everyone we know about is
    # assumed active, which is the old behaviour and the safe one.
    have_roster = bool(con.execute(
        "SELECT 1 FROM fighters WHERE on_roster IS NOT NULL LIMIT 1").fetchone())

    fighters, roster = {}, set()
    for r in con.execute("SELECT * FROM fighters"):
        wins, losses = r["wins"], r["losses"]
        if not wins and not losses:
            wins, losses = tally.get(r["fighter_id"], [0, 0])
        active = (not have_roster) or r["on_roster"] == 1
        if active:
            roster.add(r["fighter_id"])
        fighters[r["fighter_id"]] = {
            "id": r["fighter_id"], "name": r["name"],
            "div": config.DIVISIONS.get(r["division"], r["division"]),
            "rank": r["rank"], "w": wins, "l": losses, "d": r["draws"],
            "act": 1 if active else 0,
        }

    flags: dict[str, list[str]] = {}
    for r in con.execute("SELECT * FROM flags WHERE type='missed_weight'"):
        flags.setdefault(r["bout_id"], []).append(r["fighter_id"])

    stats: dict[str, dict] = {}
    for r in con.execute("SELECT * FROM bout_stats"):
        stats.setdefault(r["bout_id"], {})[r["fighter_id"]] = {
            "sig": r["sig_str_landed"], "td": r["td_landed"], "kd": r["kd"],
            "sub": r["sub_att"], "ctrl": r["ctrl_sec"], "rev": r["rev"],
        }

    events = []
    rows = con.execute(f"SELECT * FROM events e {where} ORDER BY e.date", args)
    for e in rows:
        bouts = []
        for b in con.execute(
                "SELECT * FROM bouts WHERE event_id=? ORDER BY card_position",
                (e["event_id"],)):
            if not (b["fighter_a"] and b["fighter_b"]):
                continue
            bout = {
                "id": b["bout_id"], "a": b["fighter_a"], "b": b["fighter_b"],
                "div": config.DIVISIONS.get(b["weight_class"], b["weight_class"]),
                "title": bool(b["title_bout"]),
                "rounds": 5 if (b["title_bout"] or b["card_position"] == 0) else 3,
                "done": b["status"] == "completed",
            }
            if bout["done"]:
                bout.update({
                    "win": b["winner_id"], "method": b["method"],
                    "round": b["round"], "outcome": b["outcome"],
                    "perf": any(x in json.loads(b["bonuses"] or "[]")
                                for x in ("performance", "fight_of_night",
                                          "ko_of_night", "sub_of_night")),
                    "st": stats.get(b["bout_id"], {}),
                })
            miss = flags.get(b["bout_id"])
            if miss:
                bout["miss"] = miss[0]
            bouts.append(bout)
        if not bouts:
            continue
        events.append({
            "id": e["event_id"], "name": e["name"], "date": e["date"],
            "status": e["status"], "bouts": bouts,
        })

    # The pool is everyone under contract, plus anyone who appears in a bout we
    # are shipping — a fighter released mid-season still has to render on the
    # roster that drafted them, and their past results still have to add up.
    used = {f for ev in events for b in ev["bouts"] for f in (b["a"], b["b"])}
    used |= roster
    alias = {r["from_id"]: r["to_id"] for r in
             con.execute("SELECT from_id, to_id FROM aliases")
             if r["to_id"] in fighters}
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "ufcstats.com + en.wikipedia.org",
        "fighters": [fighters[f] for f in sorted(used) if f in fighters],
        "events": events,
    }
    if alias:
        payload["alias"] = alias
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log.info("exported %s fighters (%s under contract) / %s events -> %s",
             len(payload["fighters"]),
             sum(1 for f in payload["fighters"] if f["act"]), len(events), path)
    return payload
