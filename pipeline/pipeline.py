#!/usr/bin/env python3
"""The pipeline CLI.

    python pipeline.py backfill --since 2024-01-01   # one-time history load
    python pipeline.py nightly                       # the every-night job
    python pipeline.py post-event                    # right after a card ends
    python pipeline.py verify                        # are the parsers still valid?
    python pipeline.py export                        # rebuild league_data.json
    python pipeline.py status                        # what's in the database

Two scheduled jobs, deliberately:

  post-event  runs a few hours after a card and scrapes just that event. This is
              the one that matters — it is what posts scores on Saturday night.
  nightly     re-checks every past event that is missing results, picks up newly
              announced bouts, and refreshes rankings. It is the safety net for
              when post-event fails, a card runs long, or a result is amended
              days later (an overturned decision, a failed drug test).

Neither job trusts the other. Both are idempotent.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta

import config
import normalize
import notify
import scoring
import store
from fetcher import BlockedError, Fetcher, FetchError, HostRefusing
from sources import ufcstats, wikipedia

log = logging.getLogger("pipeline")


def setup_logging(verbose: bool = False) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOG_PATH)],
    )


# --------------------------------------------------------------------- scrape
def scrape_event(con, fetcher: Fetcher, event_id: str, url: str,
                 *, with_stats: bool = True) -> dict:
    """Scrape one event and everything on it. Idempotent."""
    # The fetcher already retries and, if needed, renders a page that comes back
    # as a holding page, so anything that fails to parse here really did arrive
    # as HTML we do not understand.
    event = ufcstats.parse_event(fetcher.get(url), url=url)
    event["event_id"] = event_id

    finished = bool(event["date"]) and event["date"] <= date.today().isoformat()
    completed_bouts = [b for b in event["bouts"] if b["outcome"] in ("win", "draw", "nc")]
    event["status"] = "completed" if (finished and completed_bouts) else "scheduled"
    store.upsert_event(con, event)

    for position, bout in enumerate(event["bouts"]):
        for f in bout["fighters"]:
            if f["fighter_id"]:
                con.execute(
                    "INSERT OR IGNORE INTO fighters (fighter_id, name, updated_at) VALUES (?,?,?)",
                    (f["fighter_id"], f["name"], time.time()))

        detail = None
        if with_stats and bout["outcome"] in ("win", "draw", "nc"):
            try:
                fight_html = fetcher.get(bout["url"], immutable=True)
                detail = ufcstats.parse_fight(fight_html, url=bout["url"])
            except Exception as exc:
                # The result is already known from the event page; losing the
                # statistics costs stat-mode points on one bout, not the bout.
                log.warning("no stats for bout %s: %s", bout["bout_id"], exc)

        merged = dict(bout)
        if detail:
            merged.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
            merged["fighters"] = bout["fighters"]      # keep ufcstats ordering
            merged["winner_id"] = detail["winner_id"] or bout["winner_id"]
        merged["status"] = "completed" if merged["outcome"] in ("win", "draw", "nc") else "announced"
        store.upsert_bout(con, event_id, merged, position)

    con.commit()
    log.info("scraped %s (%s bouts, %s)", event["name"], len(event["bouts"]), event["status"])
    return event


def refresh_fighters(con, fetcher: Fetcher, limit: int | None = None) -> int:
    """Fill in career records for fighters we have only seen as a name in a bout."""
    rows = con.execute(
        "SELECT fighter_id FROM fighters WHERE wins=0 AND losses=0 ORDER BY updated_at").fetchall()
    if limit is not None:      # 0 means "skip this step", not "no limit"
        rows = rows[:limit]
    done = 0
    for r in rows:
        url = f"{config.UFCSTATS_BASE}/fighter-details/{r['fighter_id']}"
        try:
            f = ufcstats.parse_fighter(fetcher.get(url), url=url)
            store.upsert_fighter(con, f)
            done += 1
        except HostRefusing as exc:
            # Do not spend an hour retrying a decorative step against a site
            # that is refusing us. Results are already in; records can wait.
            log.warning("stopping fighter refresh after %s updated: %s", done, exc)
            break
        except Exception as exc:
            # Career records are decoration. Never let them end a run that has
            # already collected results.
            log.warning("fighter %s: %s", r["fighter_id"], exc)
    con.commit()
    return done


def refresh_rankings(con, fetcher: Fetcher) -> int:
    try:
        html = wikipedia.page_html(fetcher, config.WIKI_RANKINGS_PAGE)
        rankings = wikipedia.parse_rankings(html)
    except Exception as exc:                      # cosmetic data: never fatal
        log.warning("rankings unavailable: %s", exc)
        return 0
    return normalize.apply_rankings(con, rankings)


def refresh_roster(con, fetcher: Fetcher) -> dict:
    """Who is actually under contract. One Wikipedia request.

    Unlike the rankings this is not cosmetic: it decides who can be drafted at
    all. It still must not be fatal — a bad day at Wikipedia should not stop a
    card being scored — so a failure leaves the previous roster in place.
    """
    try:
        html = wikipedia.page_html(fetcher, config.WIKI_ROSTER_PAGE)
        roster = wikipedia.parse_roster(html)
    except Exception as exc:
        log.warning("roster unavailable: %s", exc)
        return {}
    total = sum(len(v) for v in roster.values())
    if total < 300:
        # The real roster is 600-odd across eleven divisions. Anything close to
        # empty means the article was restructured, and applying it would empty
        # the draft pool — far worse than running on last week's roster.
        log.warning("roster parse returned only %s fighters in %s divisions — "
                    "ignoring it rather than gutting the pool", total, len(roster))
        return {}
    return normalize.apply_roster(con, roster)


def enrich_from_wikipedia(con, fetcher: Fetcher, event_name: str, event_id: str) -> int:
    """Bonuses and missed-weight notes, which ufcstats does not fully carry."""
    page = event_name.split(":")[0].strip() if event_name.startswith("UFC ") else event_name
    try:
        card = wikipedia.parse_event_card(wikipedia.page_html(fetcher, page))
    except Exception as exc:
        log.info("no wikipedia page for %r: %s", page, exc)
        return 0
    return normalize.apply_wiki_notes(con, event_id, card)


def announce(con, event_id: str, event_name: str, event_date: str) -> None:
    """Tell everyone's phone that a card has been scored. Never fatal."""
    try:
        if not notify.configured():
            return
        sc = scoring.load_scoring(config.APP_BUILD)
        performances = scoring.score_event(con, event_id, sc)
        title, body = notify.event_summary(event_name, event_date, performances)
        sent = notify.send(title, body)
        if sent:
            log.info("notified via %s", ", ".join(sent))
    except Exception as exc:
        log.warning("could not send notification: %s", exc)


# ----------------------------------------------------------------- commands
def cmd_backfill(args) -> int:
    con, fetcher = store.connect(), Fetcher()
    started, problems = time.time(), []
    try:
        index = ufcstats.parse_events_index(fetcher.get(config.EVENTS_COMPLETED))
        wanted = [e for e in index if e["date"] and e["date"] >= args.since]
        done = store.known_complete_events(con)
        already = sum(1 for e in wanted if e["event_id"] in done)
        log.info("backfilling %s events since %s (%s already stored)",
                 len(wanted), args.since, already)
        for i, e in enumerate(wanted, 1):
            if e["event_id"] in done and not args.force:
                continue
            log.info("[%s/%s] %s", i, len(wanted), e["name"])
            # One unreadable event must not cost the other forty. Record it,
            # keep going, and report at the end — re-running picks up only what
            # is still missing.
            try:
                scrape_event(con, fetcher, e["event_id"], e["url"])
                enrich_from_wikipedia(con, fetcher, e["name"], e["event_id"])
            except HostRefusing as exc:
                log.error("stopping early: %s", exc)
                problems.append(f"stopped at {e['name']}: {exc}")
                break
            except Exception as exc:
                log.error("skipping %s: %s", e["name"], exc)
                problems.append(f"{e['date']}  {e['name']}: {exc}")

        # Everything above this line is the data that matters. Nothing below it
        # is allowed to throw away a run that has already collected results.
        for label, step in (("fighter records", lambda: refresh_fighters(
                                con, fetcher, limit=args.fighter_limit)),
                            ("divisions", lambda: normalize.infer_divisions(con)),
                            ("rankings", lambda: refresh_rankings(con, fetcher))):
            try:
                step()
            except Exception as exc:
                log.error("could not refresh %s: %s", label, exc)
                problems.append(f"{label}: {exc}")

        normalize.export(con, since=args.since)
        store.log_run(con, "backfill", started, not problems,
                      json.dumps(store.stats(con)) +
                      ("\nskipped:\n" + "\n".join(problems) if problems else ""))
    except Exception as exc:
        store.log_run(con, "backfill", started, False, repr(exc))
        raise
    finally:
        fetcher.close()

    print(json.dumps(store.stats(con), indent=2))
    if problems:
        print(f"\n{len(problems)} event(s) could not be read:")
        for p in problems:
            print(f"  {p}")
        print("\nEverything else is stored. Run backfill again to retry just these,\n"
              "or leave them — the nightly job keeps trying.")
    return 1 if problems else 0


def _index_or_none(fetcher: Fetcher, url: str, problems: list) -> list | None:
    """Fetch and parse an events index, or record why not and carry on."""
    try:
        return ufcstats.parse_events_index(fetcher.get(url, force=True), url=url)
    except BlockedError as exc:
        problems.append(f"{url}: refused ({exc})")
        log.warning("index unavailable: %s", exc)
    except Exception as exc:
        problems.append(f"{url}: {exc}")
        log.warning("index unusable: %s", exc)
    return None


def cmd_nightly(args) -> int:
    con, fetcher = store.connect(), Fetcher()
    started, problems = time.time(), []
    try:
        # 1. anything past-dated that still has no result
        for e in store.events_needing_results(con):
            try:
                scrape_event(con, fetcher, e["event_id"],
                             f"{config.UFCSTATS_BASE}/event-details/{e['event_id']}")
                enrich_from_wikipedia(con, fetcher, e["name"], e["event_id"])
            except Exception as exc:
                problems.append(f"{e['name']}: {exc}")

        # 2. newly announced cards
        # An index that will not load is a bad night, not a broken run: the
        # rankings, the roster and the rebuild that follow do not need ufcstats
        # at all, and a league that stops publishing because one site is
        # rate-limiting a datacenter is worse than one that publishes what it
        # has. Both indexes are therefore allowed to fail.
        for e in _index_or_none(fetcher, config.EVENTS_UPCOMING, problems) or []:
            try:
                scrape_event(con, fetcher, e["event_id"], e["url"], with_stats=False)
            except Exception as exc:
                problems.append(f"{e['name']}: {exc}")

        # 3. recent completed cards we have never seen at all
        index = _index_or_none(fetcher, config.EVENTS_COMPLETED, problems) or []
        cutoff = (date.today() - timedelta(days=args.window)).isoformat()
        known = store.known_complete_events(con)
        for e in index:
            if e["date"] and e["date"] >= cutoff and e["event_id"] not in known:
                try:
                    scrape_event(con, fetcher, e["event_id"], e["url"])
                    enrich_from_wikipedia(con, fetcher, e["name"], e["event_id"])
                except Exception as exc:
                    problems.append(f"{e['name']}: {exc}")

        refresh_fighters(con, fetcher, limit=args.fighter_limit)
        normalize.infer_divisions(con)
        refresh_rankings(con, fetcher)
        refresh_roster(con, fetcher)
        normalize.export(con, since=args.since)
        store.log_run(con, "nightly", started, not problems, "\n".join(problems) or "ok")
    finally:
        fetcher.close()
    for p in problems:
        log.error("nightly problem: %s", p)
    print(json.dumps(store.stats(con), indent=2))
    return 1 if problems else 0


def cmd_post_event(args) -> int:
    """Scrape the card that just finished. Run ~6h after the first bout."""
    con, fetcher = store.connect(), Fetcher()
    started = time.time()
    try:
        if args.event_id:
            targets = [{"event_id": args.event_id,
                        "url": f"{config.UFCSTATS_BASE}/event-details/{args.event_id}",
                        "name": args.event_id}]
        else:
            index = ufcstats.parse_events_index(fetcher.get(config.EVENTS_COMPLETED, force=True))
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            targets = [e for e in index if e["date"] in (today, yesterday)]
        if not targets:
            log.info("no card in the last 24h — nothing to do")
            store.log_run(con, "post-event", started, True, "no card")
            return 0
        for e in targets:
            ev = scrape_event(con, fetcher, e["event_id"], e["url"])
            enrich_from_wikipedia(con, fetcher, ev["name"], e["event_id"])
            missing = con.execute(
                "SELECT COUNT(*) FROM bouts WHERE event_id=? AND status!='completed'",
                (e["event_id"],)).fetchone()[0]
            if missing:
                log.warning("%s still has %s unscored bouts — nightly will retry",
                            ev["name"], missing)
            announce(con, e["event_id"], ev["name"], ev.get("date") or "")
        refresh_fighters(con, fetcher, limit=args.fighter_limit)
        normalize.infer_divisions(con)
        normalize.export(con, since=args.since)
        store.log_run(con, "post-event", started, True, json.dumps(store.stats(con)))
    except Exception as exc:
        store.log_run(con, "post-event", started, False, repr(exc))
        raise
    finally:
        fetcher.close()
    return 0


def cmd_verify(args) -> int:
    """Fetch one live page per parser and assert the shape still holds.

    Run this on a schedule too. Scrapers do not break loudly; they break by
    returning nothing, and this is what turns that into an alert.
    """
    fetcher = Fetcher()
    results, failures, blocked = [], [], []
    state = {}

    def check(name, fn, needs=None):
        if needs and needs not in state:
            results.append(("SKIP", name, f"nothing to test — {needs} check did not get that far"))
            return
        try:
            fn()
            results.append(("PASS", name, ""))
        except BlockedError:
            results.append(("BLOCKED", name, "the site served a bot check, not HTML"))
            blocked.append(name)
        except Exception as exc:
            results.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))
            failures.append(name)

    def events_index():
        rows = ufcstats.parse_events_index(fetcher.get(config.EVENTS_COMPLETED, force=True))
        assert len(rows) > 100, f"only {len(rows)} events"
        assert all(r["event_id"] and r["date"] for r in rows[:20]), "missing ids/dates"
        state["recent"] = rows[0]

    def event_page():
        e = state["recent"]
        ev = ufcstats.parse_event(fetcher.get(e["url"], force=True), url=e["url"])
        assert len(ev["bouts"]) >= 5, f"{len(ev['bouts'])} bouts on {ev['name']}"
        assert any(b["winner_id"] for b in ev["bouts"]), "no winners parsed"
        divisions = {b["weight_class"] for b in ev["bouts"]}
        assert not (divisions - set(config.DIVISIONS)), f"unknown divisions: {divisions}"
        state["bout"] = ev["bouts"][0]

    def fight_page():
        b = state["bout"]
        f = ufcstats.parse_fight(fetcher.get(b["url"], force=True), url=b["url"])
        assert f["method"] != "UNKNOWN", "method not parsed"
        assert f["stats"], "no per-fighter stats parsed"
        for st in f["stats"].values():
            assert st["sig_str_landed"] >= 0 and st["ctrl_sec"] >= 0

    def wiki_rankings():
        r = wikipedia.parse_rankings(wikipedia.page_html(fetcher, config.WIKI_RANKINGS_PAGE))
        assert len(r) >= 8, f"only {len(r)} divisions parsed"
        champs = [names[0] for names in r.values() if names]
        assert len(champs) >= 8, "divisions found but no champions in them"

    try:
        check("ufcstats events index", events_index)
        check("ufcstats event page", event_page, needs="recent")
        check("ufcstats fight page", fight_page, needs="bout")
        check("wikipedia rankings", wiki_rankings)
    finally:
        fetcher.close()

    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        print(f"{status:<8}{name:<{width}}  {detail}".rstrip())

    if blocked:
        print("\nufcstats is serving a JavaScript bot check instead of HTML. This is a\n"
              "fetching problem, not a parsing one — the parsers were never reached.\n"
              "Install the browser fallback and run verify again:\n"
              "    pip install playwright\n"
              "    python -m playwright install chromium\n"
              "If every request gets challenged, set UFC_FORCE_BROWSER=1 to skip\n"
              "the plain request and render from the start.")
    if failures:
        print(f"\n{len(failures)} parser(s) need attention. Run:\n"
              f"    python tools/dump_structure.py --wiki {config.WIKI_RANKINGS_PAGE}\n"
              "and send the output — it shows the page shape the parser is missing.")
    return 1 if (failures or blocked) else 0


def cmd_export(args) -> int:
    """Rebuild the JSON from the database. No network at all.

    Division inference runs here too: it is pure SQL over bouts we already have,
    and leaving it only in the scraping commands means an interrupted run leaves
    every fighter division-less — which is most of the app's UI.
    """
    con = store.connect()
    normalize.infer_divisions(con)
    missing = con.execute("SELECT COUNT(*) FROM fighters WHERE division=''").fetchone()[0]
    if missing:
        log.info("%s fighters still have no division (no bout in a ranked "
                 "weight class yet)", missing)
    payload = normalize.export(con, since=args.since)
    print(f"{len(payload['fighters'])} fighters, {len(payload['events'])} events "
          f"-> {config.EXPORT_PATH}")
    return 0


def cmd_rankings(args) -> int:
    """Fetch and apply the official rankings. Exactly one Wikipedia request.

    Split out from `nightly` so the draft board can be ordered without touching
    ufcstats at all — useful when it is refusing you.
    """
    con, fetcher = store.connect(), Fetcher()
    try:
        applied = refresh_rankings(con, fetcher)
    finally:
        fetcher.close()
    normalize.infer_divisions(con)
    normalize.export(con, since=args.since)
    print(f"ranked {applied} fighters")
    return 0 if applied else 1


def cmd_roster(args) -> int:
    """Refresh who is under contract. Exactly one Wikipedia request."""
    con, fetcher = store.connect(), Fetcher()
    try:
        result = refresh_roster(con, fetcher)
    finally:
        fetcher.close()
    if not result or result.get("rejected"):
        print("roster not applied — see the log")
        return 1
    normalize.export(con, since=args.since)
    print(f"{result['matched']} matched to scraped fighters, "
          f"{result['added']} new signings added, "
          f"{result['released']} placeholders dropped")
    if args.verbose and result["unmatched"]:
        print("\nno scraped record yet:")
        for n in result["unmatched"]:
            print("  ", n)
    return 0


def cmd_notify_test(args) -> int:
    """Send the alert for the most recent card, so you can check it lands on a
    phone without waiting for Saturday."""
    channels = notify.configured()
    if not channels:
        print("No notification channels configured. See .env.example.")
        return 1
    con = store.connect()
    ev = con.execute("SELECT * FROM events WHERE status='completed' "
                     "ORDER BY date DESC LIMIT 1").fetchone()
    if not ev:
        print("No scored events in the database yet.")
        return 1
    print(f"channels: {', '.join(channels)}")
    sc = scoring.load_scoring(config.APP_BUILD)
    perfs = scoring.score_event(con, ev["event_id"], sc)
    title, body = notify.event_summary(ev["name"], ev["date"], perfs)
    print(f"\n{title}\n{body}\n")
    sent = notify.send(title, body)
    print("delivered via: " + (", ".join(sent) if sent else "nothing — see the log"))
    return 0 if sent else 1


def cmd_status(args) -> int:
    con = store.connect()
    print(json.dumps(store.stats(con), indent=2))
    print("\nrecent runs:")
    for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8"):
        when = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"  {when}  {r['kind']:<11} {'ok' if r['ok'] else 'FAILED':<7} "
              f"{(r['detail'] or '')[:80]}")
    return 0


def main(argv=None) -> int:
    # Shared options are accepted on both sides of the subcommand, because
    # `backfill --since X` is what anyone would type first.  The subparser copies
    # use SUPPRESS so that an unspecified flag there does not overwrite a value
    # given before the subcommand.
    def shared(with_defaults: bool):
        q = argparse.ArgumentParser(add_help=False)
        q.add_argument("--since",
                       default="2024-01-01" if with_defaults else argparse.SUPPRESS,
                       help="earliest event date to keep in the export")
        q.add_argument("--fighter-limit", type=int,
                       default=120 if with_defaults else argparse.SUPPRESS,
                       help="max fighter pages to refresh per run")
        q.add_argument("-v", "--verbose",
                       action="store_true",
                       default=False if with_defaults else argparse.SUPPRESS)
        return q

    p = argparse.ArgumentParser(description=__doc__, parents=[shared(True)],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = shared(False)

    b = sub.add_parser("backfill", parents=[common], help="one-time history load")
    b.add_argument("--force", action="store_true")
    b.set_defaults(fn=cmd_backfill)
    n = sub.add_parser("nightly", parents=[common], help="the every-night job")
    n.add_argument("--window", type=int, default=21)
    n.set_defaults(fn=cmd_nightly)
    e = sub.add_parser("post-event", parents=[common], help="scrape the card that just ended")
    e.add_argument("--event-id")
    e.set_defaults(fn=cmd_post_event)
    sub.add_parser("verify", parents=[common], help="check the parsers still work"
                   ).set_defaults(fn=cmd_verify)
    sub.add_parser("export", parents=[common], help="rebuild league_data.json"
                   ).set_defaults(fn=cmd_export)
    sub.add_parser("notify-test", parents=[common],
                   help="send a test notification for the latest card"
                   ).set_defaults(fn=cmd_notify_test)
    sub.add_parser("rankings", parents=[common],
                   help="refresh rankings from Wikipedia (one request)"
                   ).set_defaults(fn=cmd_rankings)
    sub.add_parser("roster", parents=[common],
                   help="refresh who is under contract (one request)"
                   ).set_defaults(fn=cmd_roster)
    sub.add_parser("status", parents=[common], help="what is in the database"
                   ).set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
