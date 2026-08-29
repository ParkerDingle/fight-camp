#!/usr/bin/env python3
"""Generate a synthetic export in the exact schema `normalize.export` emits.

This exists so the app's real-data path can be exercised without waiting for a
full scrape: same keys, same types, same id style, plausible volume. It is a
test fixture generator, not a data source — never ship its output to a league.
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

DIVS = ["HW", "LHW", "MW", "WW", "LW", "FW", "BW", "FLW", "WBW", "WFLW", "WSW"]


def main(out_path: str, draft_date: str, weeks: int = 44) -> None:
    rng = random.Random(20260426)
    fighters, by_div = [], {}
    for d in DIVS:
        by_div[d] = []
        for rank in range(10):
            fid = f"{rng.getrandbits(64):016x}"
            fighters.append({"id": fid, "name": f"{d} Fighter {rank}", "div": d,
                             "rank": rank, "w": rng.randint(12, 28),
                             "l": rng.randint(1, 6), "d": 0})
            by_div[d].append(fid)

    start = date.fromisoformat(draft_date)
    events, last_fought = [], {}
    for week in range(weeks):
        day = start + timedelta(days=5 + week * 7)
        bouts, used = [], set()
        for _ in range(6):
            d = rng.choice(DIVS)
            pool = [f for f in by_div[d] if f not in used
                    and (day - last_fought.get(f, date(2000, 1, 1))).days > 80]
            if len(pool) < 2:
                continue
            a, b = rng.sample(pool, 2)
            used |= {a, b}
            last_fought[a] = last_fought[b] = day
            bout = {"id": f"{rng.getrandbits(64):016x}", "a": a, "b": b, "div": d,
                    "title": not bouts and week % 4 == 0,
                    "rounds": 5 if not bouts else 3,
                    "done": day < date.today()}
            if bout["done"]:
                win = rng.choice([a, b])
                method = rng.choice(["DEC", "DEC", "KO/TKO", "SUB"])
                bout.update({
                    "win": win, "method": method, "outcome": "win",
                    "round": bout["rounds"] if method == "DEC" else rng.randint(1, 3),
                    "perf": method != "DEC" and rng.random() < 0.4,
                    "st": {f: {"sig": rng.randint(8, 140), "td": rng.randint(0, 5),
                               "kd": rng.randint(0, 2), "sub": rng.randint(0, 3),
                               "ctrl": rng.randint(0, 600), "rev": 0}
                           for f in (a, b)},
                })
            bouts.append(bout)
        if bouts:
            events.append({"id": f"{rng.getrandbits(64):016x}",
                           "name": f"UFC {317 + week}" if week % 4 == 0
                           else f"UFC Fight Night {week}",
                           "date": day.isoformat(),
                           "status": "completed" if bouts[0]["done"] else "scheduled",
                           "bouts": bouts})

    payload = {"generated_at": "synthetic", "source": "SYNTHETIC TEST DATA",
               "fighters": fighters, "events": events}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, separators=(",", ":")))
    print(f"{len(fighters)} fighters, {len(events)} events -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(config.DATA_DIR / "synthetic.json"),
         sys.argv[2] if len(sys.argv) > 2 else "2025-10-01")
