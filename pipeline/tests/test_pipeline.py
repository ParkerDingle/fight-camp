"""Integration test for the weekly path: scrape_event -> database -> export.

pipeline.scrape_event is the function that actually runs every Saturday night,
so it gets tested against fixtures with a fake fetcher rather than being left
to find out in production. The fake deliberately has stats for only one of the
two bouts, which exercises both the happy path and the "fight page unavailable,
score it on outcome alone" fallback.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import normalize     # noqa: E402
import pipeline      # noqa: E402
import store         # noqa: E402
from fetcher import FetchError   # noqa: E402

FIX = Path(__file__).parent / "fixtures"
EVENT_URL = "http://ufcstats.com/event-details/aaa1111111111111"
WITH_STATS = "http://ufcstats.com/fight-details/f00d000000000002"


class FakeFetcher:
    """Serves fixtures; refuses the fight page we want to fail."""

    def __init__(self):
        self.calls = []

    def get(self, url, *, immutable=False, force=False):
        self.calls.append(url)
        if "event-details" in url:
            return (FIX / "event.html").read_text()
        if url == WITH_STATS:
            return (FIX / "fight.html").read_text()
        if "fight-details" in url:
            raise FetchError("simulated 503")
        raise AssertionError(f"unexpected fetch: {url}")

    def close(self):
        pass


class TestScrapeEvent(unittest.TestCase):
    def setUp(self):
        self.con = store.connect(":memory:")
        self.fetcher = FakeFetcher()
        self.event = pipeline.scrape_event(
            self.con, self.fetcher, "aaa1111111111111", EVENT_URL)

    def test_event_marked_completed(self):
        row = self.con.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["date"], "2026-08-16")

    def test_both_bouts_stored_with_card_position(self):
        rows = list(self.con.execute("SELECT * FROM bouts ORDER BY card_position"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["card_position"], 0)
        self.assertEqual(rows[0]["winner_id"], "aaa000000000000a")
        self.assertTrue(rows[0]["title_bout"])
        self.assertEqual(rows[1]["method"], "KO/TKO")
        self.assertTrue(all(r["status"] == "completed" for r in rows))

    def test_fighters_created_from_bout_rows(self):
        names = {r["name"] for r in self.con.execute("SELECT name FROM fighters")}
        self.assertEqual(names, {"Khamzat Chimaev", "Dricus Du Plessis",
                                 "Lerone Murphy", "Aaron Pico"})

    def test_stats_only_for_the_bout_that_had_a_page(self):
        with_stats = {r["bout_id"] for r in
                      self.con.execute("SELECT DISTINCT bout_id FROM bout_stats")}
        self.assertEqual(with_stats, {"f00d000000000002"})

    def test_failed_fight_page_does_not_lose_the_result(self):
        """A missing stats page must not cost us the win — outcome scoring still
        works, which is the whole reason the two are stored separately."""
        row = self.con.execute(
            "SELECT * FROM bouts WHERE bout_id='f00d000000000001'").fetchone()
        self.assertEqual(row["winner_id"], "aaa000000000000a")
        self.assertEqual(row["method"], "DEC")
        self.assertEqual(row["status"], "completed")

    def test_rerun_is_idempotent(self):
        before = store.stats(self.con)
        pipeline.scrape_event(self.con, FakeFetcher(), "aaa1111111111111", EVENT_URL)
        self.assertEqual(before, store.stats(self.con))

    def test_export_is_app_ready(self):
        normalize.infer_divisions(self.con)
        payload = normalize.export(self.con, Path("/tmp/_pipeline_export.json"),
                                   since="2020-01-01")
        ids = {f["id"] for f in payload["fighters"]}
        for event in payload["events"]:
            for bout in event["bouts"]:
                self.assertIn(bout["a"], ids)
                self.assertIn(bout["b"], ids)
                self.assertTrue(bout["done"])
        main = payload["events"][0]["bouts"][0]
        self.assertEqual(main["rounds"], 5)
        self.assertEqual(payload["events"][0]["bouts"][1]["rounds"], 3)


class BackfillFetcher:
    """Two events in the index; the second one serves a bot check instead of a
    page, in both plain and rendered form."""

    BAD = "<html><body>Just a moment…</body></html>"

    def get(self, url, *, immutable=False, force=False, render=False):
        if "statistics/events" in url:
            return (FIX / "events_index.html").read_text()
        if "event-details/aaa1111111111111" in url:
            return (FIX / "event.html").read_text()
        if "event-details/" in url:
            return self.BAD
        if url == WITH_STATS:
            return (FIX / "fight.html").read_text()
        raise FetchError("simulated failure")

    def get_json(self, url, params):
        raise FetchError("wikipedia unavailable in this test")

    def close(self):
        pass


class TestBackfillResilience(unittest.TestCase):
    """One unreadable event used to abort the whole run and lose the other
    forty. It must be recorded and skipped instead."""

    def setUp(self):
        import config
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self._db, self._export = config.DB_PATH, config.EXPORT_PATH
        config.DB_PATH = self.tmp / "ufc.db"
        config.EXPORT_PATH = self.tmp / "league_data.json"
        self._fetcher = pipeline.Fetcher
        pipeline.Fetcher = BackfillFetcher

    def tearDown(self):
        import config
        config.DB_PATH, config.EXPORT_PATH = self._db, self._export
        pipeline.Fetcher = self._fetcher

    def test_bad_event_does_not_lose_the_good_one(self):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(since="2020-01-01", fighter_limit=0, force=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.cmd_backfill(args)

        self.assertEqual(code, 1, "a skipped event should be a non-zero exit")
        self.assertIn("could not be read", out.getvalue())

        con = store.connect(self.tmp / "ufc.db")
        stats = store.stats(con)
        self.assertEqual(stats["completed_events"], 1)
        self.assertEqual(stats["bouts"], 2)
        self.assertTrue(config_export_written(self.tmp))

    def test_rerun_retries_only_the_missing_event(self):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(since="2020-01-01", fighter_limit=0, force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.cmd_backfill(args)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pipeline.cmd_backfill(args)
        self.assertEqual(code, 1)
        con = store.connect(self.tmp / "ufc.db")
        self.assertEqual(store.stats(con)["completed_events"], 1)


def config_export_written(tmp: Path) -> bool:
    return (tmp / "league_data.json").exists()


if __name__ == "__main__":
    unittest.main(verbosity=2)
