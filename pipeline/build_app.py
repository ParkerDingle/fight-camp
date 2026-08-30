#!/usr/bin/env python3
"""Inject the scraped dataset into the app and write a deployable page.

    python build_app.py --app ../octagon-draft.html --out dist/octagon-draft.html \
        --draft-date 2026-09-05 --roster-size 10

The app ships with generated demo data and swaps to real data the moment a
non-empty `<script id="league-data">` block is present. This writes that block.

It also resets the embedded league state, because a league drafted against demo
fighter ids cannot be carried over onto real ones — the rosters would point at
fighters that no longer exist. A real dataset means a real draft.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import config

DATA_TAG = re.compile(
    r'(<script id="league-data" type="application/json">)(.*?)(</script>)', re.S)
STATE_TAG = re.compile(
    r'(<script id="league-state" type="application/json">)(.*?)(</script>)', re.S)

MAX_BYTES = 4_000_000      # the page must stay comfortably under the 16MB cap


def load_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    fighters = {f["id"] for f in payload["fighters"]}
    dropped = 0
    for event in payload["events"]:
        keep = [b for b in event["bouts"] if b["a"] in fighters and b["b"] in fighters]
        dropped += len(event["bouts"]) - len(keep)
        event["bouts"] = keep
    payload["events"] = [e for e in payload["events"] if e["bouts"]]
    if dropped:
        print(f"  note: dropped {dropped} bouts referencing unknown fighters")
    no_div = [f["name"] for f in payload["fighters"] if not f.get("div")]
    if no_div:
        print(f"  note: {len(no_div)} fighters have no division "
              f"(e.g. {', '.join(no_div[:3])}) — run `pipeline.py nightly` to infer")
    return payload


def trim_to_season(payload: dict, draft_date: str, months: int,
                   history_months: int = 12) -> dict:
    """Keep the season window, plus enough history to have a draft pool.

    Two different windows, and conflating them empties the app:

      scoring   draft day -> draft day + 12 months. The app only counts bouts a
                fighter had while on your roster, so nothing before the draft
                can score anyway.
      the pool  the 12 months *before* the draft. That is what makes someone
                draftable — a fighter who fought last month is a known
                quantity even though that bout will never score.

    Keeping only the scoring window means a league drafted today has no fighters
    in it at all, because none of them have fought in the future yet.
    """
    start = datetime.fromisoformat(draft_date).date()
    end = start.replace(year=start.year + (months // 12))
    history_start = start.replace(year=start.year - max(1, history_months // 12))

    payload["events"] = [e for e in payload["events"]
                         if e["date"] and history_start.isoformat() <= e["date"]
                         <= end.isoformat()]
    used = {f for e in payload["events"] for b in e["bouts"] for f in (b["a"], b["b"])}
    # Three reasons to keep a fighter: they fought inside the window, they are
    # ranked, or they are under contract. The last one is what makes a champion
    # coming back from a year out draftable — trimming to "who fought recently"
    # is exactly the hole the roster pass exists to fill, and re-cutting it here
    # would quietly undo that work.
    payload["fighters"] = [f for f in payload["fighters"]
                           if f["id"] in used or f.get("rank") is not None
                           or f.get("act") == 1]

    scoring = [e for e in payload["events"] if e["date"] >= start.isoformat()]
    print(f"  pool from {history_start} · scoring from {start} "
          f"({len(scoring)} event(s) inside the season so far)")
    return payload


def build_state(existing: dict, args, payload: dict) -> dict:
    state = dict(existing)
    draft_ts = int(datetime.fromisoformat(args.draft_date)
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    state["rev"] = existing.get("rev", 1) + 1
    state["seed"] = bool(args.auto_draft)
    state["picks"] = []
    state["rosters"] = {}
    state["banked"] = {}
    state["trades"] = []
    state["log"] = []
    state["fouls"] = {}
    import secrets
    league = dict(existing.get("league", {}))
    state["league"] = dict(league, **{
        "syncId": league.get("syncId") or secrets.token_hex(8),
        "name": args.league_name,
        "code": args.code,
        "rosterSize": args.roster_size,
        "draftTs": draft_ts,
        "draftMode": "complete" if args.auto_draft else "pre",
        "commissioner": league.get("commissioner") or "m1",
    })
    if args.managers:
        names = [n.strip() for n in args.managers.split(",") if n.strip()]
        palette = ["#B8332C", "#26548F", "#2C7A57", "#9C7A26", "#6D4C9F",
                   "#B5651D", "#0F7C8A", "#8A3D6B", "#4A5A6B", "#7A2E4A"]
        state["managers"] = [
            {"id": f"m{i+1}", "name": n, "color": palette[i % len(palette)], "claimed": False}
            for i, n in enumerate(names)]
    return state


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app", default=str(Path(__file__).resolve().parent / "app" / "octagon-draft.html"),
                   help="the app template to inject into (ships in app/)")
    p.add_argument("--data", default=str(config.EXPORT_PATH))
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "dist" / "octagon-draft.html"))
    p.add_argument("--draft-date", default=datetime.now().date().isoformat(),
                   help="YYYY-MM-DD; anchors the rolling 12-month season")
    p.add_argument("--season-months", type=int, default=12)
    p.add_argument("--history-months", type=int, default=12,
                   help="how much past form to ship as the draft pool")
    p.add_argument("--roster-size", type=int, default=10)
    p.add_argument("--league-name", default="The Fight Camp")
    p.add_argument("--code", default="UFC-4K2Q")
    p.add_argument("--managers", default="",
                   help="comma-separated manager names; omit to keep the current seats")
    p.add_argument("--auto-draft", action="store_true",
                   help="pre-fill rosters by best-available instead of opening a live draft")
    args = p.parse_args(argv)

    app_path, data_path, out_path = Path(args.app), Path(args.data), Path(args.out)
    if not app_path.exists():
        print(f"missing app template: {app_path}\n"
              "It ships in app/octagon-draft.html — pass --app if yours lives elsewhere.",
              file=sys.stderr)
        return 1
    if not data_path.exists():
        print(f"missing dataset: {data_path}\n"
              "Nothing has been scraped yet. Run:\n"
              "    python pipeline.py verify\n"
              "    python pipeline.py backfill --since 2025-09-01",
              file=sys.stderr)
        return 1

    print(f"reading  {data_path}")
    payload = trim_to_season(load_payload(data_path), args.draft_date,
                             args.season_months, args.history_months)
    print(f"  {len(payload['fighters'])} fighters, {len(payload['events'])} events "
          f"in the season starting {args.draft_date}")
    if not payload["fighters"]:
        print("refusing to build: no fighters in the season window.\n"
              "Either the database is empty (run `pipeline.py backfill`) or "
              "--draft-date is outside the range of scraped events.", file=sys.stderr)
        return 1

    html = app_path.read_text(encoding="utf-8")
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    if not DATA_TAG.search(html):
        print("app has no <script id=\"league-data\"> block", file=sys.stderr)
        return 1
    html = DATA_TAG.sub(lambda m: m.group(1) + data_json + m.group(3), html, count=1)

    # The page is titled after the league, so a published copy is identifiable
    # in a browser tab and an artifact gallery.
    html = re.sub(r"<title>.*?</title>",
                  lambda _: f"<title>{args.league_name}</title>", html, count=1)
    # The name under the icon when someone adds it to their home screen.
    html = re.sub(r'(<meta name="apple-mobile-web-app-title" content=")[^"]*(">)',
                  lambda m: m.group(1) + args.league_name + m.group(2), html, count=1)

    state_match = STATE_TAG.search(html)
    if not state_match:
        print("app has no <script id=\"league-state\"> block", file=sys.stderr)
        return 1
    state = build_state(json.loads(state_match.group(2)), args, payload)
    state_json = json.dumps(state, separators=(",", ":")).replace("</", "<\\/")
    html = STATE_TAG.sub(lambda m: m.group(1) + "\n" + state_json + "\n" + m.group(3),
                         html, count=1)

    size = len(html.encode())
    if size > MAX_BYTES:
        print(f"warning: page is {size/1e6:.1f}MB — trim the season window "
              f"or drop unranked fighters", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote    {out_path}  ({size/1024:.0f} KB)")
    print(f"         draft {'pre-filled' if args.auto_draft else 'closed until the commissioner opens it'}, "
          f"{args.roster_size}-fighter rosters")
    print("\nPublish it, then send the link to your league.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
