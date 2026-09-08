"""A Python mirror of the app's scoring rules.

Why this exists: the notifier wants to say "Gaethje put up 318 last night", and
the only place that number lives is inside the page's JavaScript. Rather than
guess, this reimplements the same rules — and reads the same values out of the
built page, so a change made in the app's Scoring tab is reflected here without
anyone editing two files.

The *values* have one source of truth. The *formula* is written twice, once in
JS and once here, which is a real duplication risk — so tests/test_scoring.py
pins this against numbers taken from the running app. If you change the formula
in the app, that test fails, which is the point.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


def _r1(n: float) -> float:
    """Round to one decimal the way JavaScript's Math.round does — halves go
    toward +infinity. Python's round() uses banker's rounding, which puts this
    file 0.1 out from the app on any value landing exactly on a half."""
    return math.floor(n * 10 + 0.5) / 10

SCORING_VERSION = 2

# Kept in step with DEFAULT_SCORING in app/octagon-draft.html. These are only a
# fallback: load_scoring() reads the real numbers out of the built page, so a
# change made in the app's Scoring tab reaches the notifier without anyone
# editing this file. tests/test_scoring.py pins the two together.
DEFAULTS = {
    "appear": 2,
    "winDec": 4, "winSub": 7, "winKo": 8,
    "fin1": 3, "fin2": 2, "fin3": 1, "fin4": 0.5,
    "lossDec": -1, "lossFin": -2, "draw": 1, "perf": 2,
    "titleMult": 1.25, "missWeight": -2,
    "statsOn": True, "sig": 0.03, "td": 0.3, "kd": 1, "subatt": 0.4, "ctrl": 0.1,
    "oppC": 2, "oppT5": 1.75, "oppT10": 1.5, "oppT15": 1.25, "oppUR": 1,
    # Fouls are logged by hand in the app — nobody publishes them as data, so
    # score_event() never applies them. They are carried here anyway so the two
    # tables are identical and the parity test can say so.
    "foulEye": -1.5, "foulGroin": -1, "foulOther": -0.5, "foulPoint": -2.5,
}

_STATE_TAG = re.compile(
    r'<script id="league-state" type="application/json">(.*?)</script>', re.S)


def load_scoring(app_path: Path | str | None = None) -> dict:
    """Read the live scoring values out of a built page; fall back to defaults."""
    values = dict(DEFAULTS)
    if not app_path:
        return values
    try:
        m = _STATE_TAG.search(Path(app_path).read_text(encoding="utf-8"))
        if m:
            values.update(json.loads(m.group(1).replace("<\\/", "</")).get("scoring", {}))
    except Exception:
        pass
    return values


def opponent_multiplier(rank, sc: dict) -> tuple[float, str]:
    if rank is None or rank >= 99:
        return sc["oppUR"], "unranked opponent"
    if rank == 0:
        return sc["oppC"], "champion"
    if rank <= 5:
        return sc["oppT5"], f"top 5 (#{rank})"
    if rank <= 10:
        return sc["oppT10"], f"top 10 (#{rank})"
    return sc["oppT15"], f"ranked #{rank}"


def method_class(method: str) -> str:
    """Mirrors methodClass() in the app.

    ufcstats reports more than three endings — disqualifications, doctor
    stoppages, "Could Not Continue", overturned results. Treating everything
    that is not a decision or a submission as a knockout paid knockout money and
    a finish bonus for all of them.
    """
    m = (method or "").upper()
    if "KO" in m:
        return "ko"
    if "SUB" in m:
        return "sub"
    if "DEC" in m:
        return "dec"
    if "DQ" in m:
        return "dq"
    return "other"


def bout_points(bout: dict, fighter_id: str, opponent_rank, sc: dict) -> dict:
    """bout: {done, winner_id, method, round, title, perf, missed_weight,
    outcome, stats{}}"""
    lines: list[tuple[str, float]] = []
    total = 0.0

    def add(label, value):
        # Rounded as it lands, and the total is the sum of the rounded lines, so
        # a scorecard adds up to the number printed at the bottom of it. Mirrors
        # the app exactly.
        nonlocal total
        value = _r1(value)
        if value:
            total = _r1(total + value)
            lines.append((label, value))

    if bout.get("done"):
        outcome = bout.get("outcome") or ("win" if bout.get("winner_id") else "")
        add("Fought", sc["appear"])
        if outcome == "draw":
            add("Draw", sc.get("draw", 0))
        elif outcome == "nc":
            lines.append(("No contest", 0))
        elif bout.get("winner_id"):
            mult = sc["titleMult"] if bout.get("title") else 1
            method = bout.get("method", "DEC")
            mc = method_class(method)
            if bout["winner_id"] == fighter_id:
                base = sc["winKo"] if mc == "ko" else (
                    sc["winSub"] if mc == "sub" else sc["winDec"])
                add(f"Win by {method}", _r1(base * mult))
                if mc in ("ko", "sub"):
                    rnd = bout.get("round") or 1
                    fin = {1: sc["fin1"], 2: sc["fin2"], 3: sc["fin3"]}.get(rnd, sc["fin4"])
                    add(f"Round {rnd} finish", _r1(fin * mult))
                if bout.get("perf"):
                    add("Performance bonus", sc["perf"])
            else:
                add(f"Loss by {method}",
                    sc["lossFin"] if mc in ("ko", "sub", "dq") else sc["lossDec"])

        st = (bout.get("stats") or {}).get(fighter_id)
        if sc.get("statsOn") and st:
            add(f"Significant strikes ({st['sig']})", _r1(st["sig"] * sc["sig"]))
            add(f"Takedowns ({st['td']})", st["td"] * sc["td"])
            add(f"Knockdowns ({st['kd']})", st["kd"] * sc["kd"])
            add(f"Submission attempts ({st['sub']})", st["sub"] * sc["subatt"])
            add(f"Control time ({round(st['ctrl'] / 60)}m)",
                _r1((st["ctrl"] / 60) * sc["ctrl"]))

        mult, label = opponent_multiplier(opponent_rank, sc)
        if mult and mult != 1 and total != 0:
            scaled = _r1(total * mult if total > 0 else total / mult)
            lines.append((f"vs {label} (×{mult})", _r1(scaled - total)))
            total = scaled

    if bout.get("missed_weight"):
        add("Missed weight", sc["missWeight"])
    return {"points": _r1(total), "lines": lines}


def score_event(con, event_id: str, sc: dict) -> list[dict]:
    """Every fighter on one card, best performance first."""
    out = []
    ranks = {r["fighter_id"]: r["rank"]
             for r in con.execute("SELECT fighter_id, rank FROM fighters")}
    names = {r["fighter_id"]: r["name"]
             for r in con.execute("SELECT fighter_id, name FROM fighters")}
    flags = {(r["bout_id"], r["fighter_id"])
             for r in con.execute("SELECT bout_id, fighter_id FROM flags "
                                  "WHERE type='missed_weight'")}
    for b in con.execute("SELECT * FROM bouts WHERE event_id=? AND status='completed'",
                         (event_id,)):
        stats = {r["fighter_id"]: {"sig": r["sig_str_landed"], "td": r["td_landed"],
                                   "kd": r["kd"], "sub": r["sub_att"], "ctrl": r["ctrl_sec"]}
                 for r in con.execute("SELECT * FROM bout_stats WHERE bout_id=?",
                                      (b["bout_id"],))}
        bonuses = json.loads(b["bonuses"] or "[]")
        for fid, opp in ((b["fighter_a"], b["fighter_b"]), (b["fighter_b"], b["fighter_a"])):
            if not fid:
                continue
            payload = {
                "done": True, "winner_id": b["winner_id"], "method": b["method"],
                "round": b["round"], "title": bool(b["title_bout"]),
                "outcome": b["outcome"],
                "perf": bool(bonuses), "stats": stats,
                "missed_weight": (b["bout_id"], fid) in flags,
            }
            res = bout_points(payload, fid, ranks.get(opp), sc)
            out.append({"fighter_id": fid, "name": names.get(fid, "?"),
                        "points": res["points"], "won": b["winner_id"] == fid,
                        "opponent": names.get(opp, "?")})
    out.sort(key=lambda r: r["points"], reverse=True)
    return out
