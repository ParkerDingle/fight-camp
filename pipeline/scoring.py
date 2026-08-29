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

DEFAULTS = {
    "appear": 25,
    "winDec": 50, "winSub": 80, "winKo": 90,
    "fin1": 30, "fin2": 20, "fin3": 10, "fin4": 5,
    "lossDec": -10, "lossFin": -20, "perf": 25,
    "titleMult": 1.25, "missWeight": -20,
    "statsOn": True, "sig": 0.4, "td": 4, "kd": 12, "subatt": 5, "ctrl": 1.5,
    "oppC": 2, "oppT5": 1.75, "oppT10": 1.5, "oppT15": 1.25, "oppUR": 1,
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


def bout_points(bout: dict, fighter_id: str, opponent_rank, sc: dict) -> dict:
    """bout: {done, winner_id, method, round, title, perf, missed_weight, stats{}}"""
    lines: list[tuple[str, float]] = []
    total = 0.0

    def add(label, value):
        nonlocal total
        if value:
            total += value
            lines.append((label, _r1(value)))

    if bout.get("done"):
        add("Fought", sc["appear"])
        if bout.get("winner_id"):
            mult = sc["titleMult"] if bout.get("title") else 1
            if bout["winner_id"] == fighter_id:
                method = bout.get("method", "DEC")
                base = sc["winDec"] if method == "DEC" else (
                    sc["winSub"] if method == "SUB" else sc["winKo"])
                add(f"Win by {method}", _r1(base * mult))
                if method != "DEC":
                    rnd = bout.get("round") or 1
                    fin = {1: sc["fin1"], 2: sc["fin2"], 3: sc["fin3"]}.get(rnd, sc["fin4"])
                    add(f"Round {rnd} finish", _r1(fin * mult))
                if bout.get("perf"):
                    add("Performance bonus", sc["perf"])
            else:
                add(f"Loss by {bout.get('method','DEC')}",
                    sc["lossDec"] if bout.get("method") == "DEC" else sc["lossFin"])

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
            scaled = total * mult if total > 0 else total / mult
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
                "perf": bool(bonuses), "stats": stats,
                "missed_weight": (b["bout_id"], fid) in flags,
            }
            res = bout_points(payload, fid, ranks.get(opp), sc)
            out.append({"fighter_id": fid, "name": names.get(fid, "?"),
                        "points": res["points"], "won": b["winner_id"] == fid,
                        "opponent": names.get(opp, "?")})
    out.sort(key=lambda r: r["points"], reverse=True)
    return out
