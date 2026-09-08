"""What is a fighter likely to be worth over the next twelve months?

The league is decided by cumulative points, and the draft is the only moment
anybody gets to act on a whole season at once. So the question worth answering
is not "who is best" but "who will score most", and those are different
questions: a champion who defends once a year loses to a gatekeeper who fights
three times.

    expected season points  =  expected fights  x  expected points per fight

Both halves come from the last twelve months of real results, run through the
league's own scoring engine, and both are shrunk toward the divisional average
because most fighters have one or two bouts on record and one or two bouts is
not evidence. A fighter with a single lucky knockout should not out-project a
proven contender on the strength of it.

Two deliberate choices worth knowing about:

Statistics are modelled, not read. The stats in the database are unreliable for
every bout scraped before the totals-table fix — they hold round one's numbers
rather than the fight's, which undercounts a decision by about seventy percent.
Rather than project from figures known to be wrong, the stat component is
estimated from fight duration, fitted on the bouts that were re-scraped
correctly. It is the weakest part of the model and it is meant to be: it
contributes a couple of points a fight, and a wrong guess there moves a season
projection by single digits.

Ranks are current, not historical. The opponent multiplier uses the rank a
fighter holds now, exactly as the app's scoring does, so a projection and a
scorecard never disagree about what a win was worth.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date, datetime

import config
import scoring
import store

log = logging.getLogger("projections")

# Cards re-scraped after the totals-table fix in sources/ufcstats.py. Only these
# carry whole-fight statistics; everything else holds round one's numbers.
TRUSTED_STAT_EVENTS = (
    "2144954270be834d", "9d61d8cb1c354867", "6436029b50a9c255", "30ad2050273d016a",
)

# How much evidence one fighter's own record needs before it outweighs the
# divisional average. Two fights: with one bout on record the estimate sits a
# third of the way from the average toward what they did; with six it is
# three-quarters of the way.
PRIOR_WEIGHT_FIGHTS = 2.0
PRIOR_WEIGHT_POINTS = 2.0

WINDOW_DAYS = 365


def _minutes(round_no: int, clock: str) -> float:
    """How long the bout lasted, in minutes.

    A missing or unparseable clock means the middle of the round rather than
    zero: a bout that happened took some time, and calling it instantaneous
    would quietly hand that fighter the smallest possible stat allowance.
    """
    rounds_done = (int(round_no or 1) - 1) * 5
    try:
        mm, ss = (clock or "").split(":")
        within = int(mm) + int(ss) / 60
    except Exception:
        within = 0.0
    if within <= 0:
        within = 2.5
    return max(0.0, rounds_done + within)


def fit_stat_model(con) -> tuple[float, float]:
    """stat_points = a + b * minutes, fitted on the bouts we trust.

    Falls back to a flat league average if there is nothing trustworthy to fit,
    which is better than propagating numbers known to be wrong.
    """
    sc = scoring.DEFAULTS
    xs, ys = [], []
    placeholders = ",".join("?" * len(TRUSTED_STAT_EVENTS))
    rows = con.execute(
        f"""SELECT b.round, b.time, s.sig_str_landed, s.td_landed, s.kd,
                   s.sub_att, s.ctrl_sec
            FROM bouts b JOIN bout_stats s ON s.bout_id = b.bout_id
            WHERE b.event_id IN ({placeholders}) AND b.status = 'completed'""",
        TRUSTED_STAT_EVENTS)
    for r in rows:
        xs.append(_minutes(r["round"], r["time"]))
        ys.append(r["sig_str_landed"] * sc["sig"] + r["td_landed"] * sc["td"]
                  + r["kd"] * sc["kd"] + r["sub_att"] * sc["subatt"]
                  + (r["ctrl_sec"] / 60) * sc["ctrl"])
    if len(xs) < 20:
        log.warning("only %s trustworthy stat rows — using a flat stat allowance", len(xs))
        return (statistics.mean(ys) if ys else 1.8), 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    return my - b * mx, b


def _bout_points(row, fighter_id, opp_rank, sc, stat_model) -> float:
    """One bout, scored the way the app scores it, with modelled statistics."""
    a, b = stat_model
    payload = {
        "done": True,
        "winner_id": row["winner_id"],
        "method": row["method"],
        "round": row["round"],
        "title": bool(row["title_bout"]),
        "outcome": row["outcome"],
        "perf": bool(json.loads(row["bonuses"] or "[]")),
        "stats": {},
    }
    pts = scoring.bout_points(payload, fighter_id, opp_rank, sc)["points"]
    # The stat block is added inside the opponent multiplier, as it is in the
    # app, so a big night against the champion scales the whole thing.
    stat_pts = max(0.0, a + b * _minutes(row["round"], row["time"]))
    mult, _ = scoring.opponent_multiplier(opp_rank, sc)
    return pts + stat_pts * (mult or 1)


def build(con, as_of: date | None = None, window_days: int = WINDOW_DAYS) -> dict:
    """Return {fighter_id: {...}} for every fighter under contract."""
    sc = scoring.DEFAULTS
    stat_model = fit_stat_model(con)
    as_of = as_of or date.today()
    cutoff = date.fromordinal(as_of.toordinal() - window_days).isoformat()

    ranks = {r["fighter_id"]: r["rank"] for r in con.execute(
        "SELECT fighter_id, rank FROM fighters")}
    divisions = {r["fighter_id"]: r["division"] for r in con.execute(
        "SELECT fighter_id, division FROM fighters")}
    active = {r["fighter_id"] for r in con.execute(
        "SELECT fighter_id FROM fighters WHERE on_roster = 1")}
    if not active:                      # roster pass never ran; treat all as active
        active = set(ranks)

    per_fighter: dict[str, list[float]] = {}
    for row in con.execute(
            """SELECT b.*, e.date FROM bouts b JOIN events e ON e.event_id = b.event_id
               WHERE b.status = 'completed' AND e.date >= ? AND e.date <= ?""",
            (cutoff, as_of.isoformat())):
        for fid, opp in ((row["fighter_a"], row["fighter_b"]),
                         (row["fighter_b"], row["fighter_a"])):
            if not fid:
                continue
            per_fighter.setdefault(fid, []).append(
                _bout_points(row, fid, ranks.get(opp), sc, stat_model))

    # Already-announced bouts are the strongest signal a fighter is active.
    booked: dict[str, int] = {}
    for row in con.execute(
            """SELECT b.fighter_a, b.fighter_b FROM bouts b
               JOIN events e ON e.event_id = b.event_id
               WHERE b.status = 'announced' AND e.date > ?""", (as_of.isoformat(),)):
        for fid in (row["fighter_a"], row["fighter_b"]):
            if fid:
                booked[fid] = booked.get(fid, 0) + 1

    # Divisional priors, computed over fighters who actually fought.
    by_div_fights: dict[str, list[float]] = {}
    by_div_points: dict[str, list[float]] = {}
    for fid, pts in per_fighter.items():
        d = divisions.get(fid, "")
        by_div_fights.setdefault(d, []).append(len(pts))
        by_div_points.setdefault(d, []).extend(pts)
    all_fights = [len(v) for v in per_fighter.values()] or [1.0]
    all_points = [p for v in per_fighter.values() for p in v] or [8.0]
    global_fights, global_points = statistics.mean(all_fights), statistics.mean(all_points)

    def prior(bucket, div, fallback):
        vals = bucket.get(div) or []
        return statistics.mean(vals) if len(vals) >= 5 else fallback

    out = {}
    for fid in active:
        pts = per_fighter.get(fid, [])
        n = len(pts)
        div = divisions.get(fid, "")

        pf_prior = prior(by_div_fights, div, global_fights)
        ppf_prior = prior(by_div_points, div, global_points)

        # Shrinkage: a fighter's own record counts for more the more of it
        # there is. With nothing on record they are simply an average fighter
        # in their division, which is the honest answer.
        fights = (n * n + PRIOR_WEIGHT_FIGHTS * pf_prior) / (n + PRIOR_WEIGHT_FIGHTS)
        ppf = ((sum(pts) + PRIOR_WEIGHT_POINTS * ppf_prior)
               / (n + PRIOR_WEIGHT_POINTS))
        # A booked bout is a fight that is close to certain; nudge, do not double.
        if booked.get(fid):
            fights = max(fights, 1.0) + 0.35 * min(booked[fid], 2)

        proj = fights * ppf
        # A spread, so the number is not read as a promise. Driven by how much
        # the fighter's own nights actually vary, widened when there is little
        # to go on.
        spread = statistics.pstdev(pts) if n >= 2 else ppf * 0.8
        conf = n / (n + 3.0)
        lo = max(0.0, proj - fights ** 0.5 * spread - (1 - conf) * proj * 0.35)
        hi = proj + fights ** 0.5 * spread + (1 - conf) * proj * 0.35

        out[fid] = {
            "proj": round(proj, 1),
            "lo": round(lo, 1),
            "hi": round(hi, 1),
            "pf": round(fights, 2),      # expected fights
            "ppf": round(ppf, 1),        # expected points per fight
            "n": n,                      # bouts the estimate is built on
            "booked": booked.get(fid, 0),
        }
    log.info("projected %s fighters (stat model: %.2f + %.4f/min)",
             len(out), stat_model[0], stat_model[1])
    return out
