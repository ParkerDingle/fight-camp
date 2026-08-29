"""Tests for throttle detection.

The failure these cover cost a real backfill 17 of 41 events. ufcstats does not
rate-limit with HTTP 429 — it answers 200 OK with a ~385-byte page titled
"Stats | UFC" that redirects with JavaScript. Nothing in it says "blocked", so
without this heuristic it reads as a parser failure and the run degrades
silently the longer it goes on.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config          # noqa: E402
import fetcher as fetcher_mod   # noqa: E402
from fetcher import BlockedError, Fetcher   # noqa: E402

# What the site actually returns when it is throttling you.
HOLDING_PAGE = (
    '<!DOCTYPE html><html><head><title>Stats | UFC</title>'
    '<script type="text/javascript">window.location.reload(true);</script>'
    '</head><body></body></html>' + " " * 220
)
CLOUDFLARE = "<html><head><title>Just a moment...</title></head><body></body></html>"
REAL_PAGE = ('<html><body><h2 class="b-content__title">'
             '<span class="b-content__title-highlight">UFC 300</span></h2>'
             '<table class="b-fight-details__table"></table>' + "x" * 3000 +
             '</body></html>')

EVENT_URL = "http://ufcstats.com/event-details/abc123"


class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.headers = {}


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return FakeResponse(self.script.pop(0) if self.script else REAL_PAGE)

    def close(self):
        pass


class FetcherTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.slept = []
        self._sleep = time.sleep
        fetcher_mod.time.sleep = lambda s: self.slept.append(s)

    def tearDown(self):
        fetcher_mod.time.sleep = self._sleep


class TestBlockDetection(FetcherTestCase):
    def test_recognises_the_silent_holding_page(self):
        f = Fetcher(cache_dir=self.tmp)
        self.assertTrue(f._looks_blocked(EVENT_URL, HOLDING_PAGE))

    def test_recognises_cloudflare(self):
        f = Fetcher(cache_dir=self.tmp)
        self.assertTrue(f._looks_blocked(EVENT_URL, CLOUDFLARE))

    def test_real_page_is_not_blocked(self):
        f = Fetcher(cache_dir=self.tmp)
        self.assertFalse(f._looks_blocked(EVENT_URL, REAL_PAGE))

    def test_short_pages_off_ufcstats_are_left_alone(self):
        """The size heuristic is specific to one site; do not apply it to a
        legitimately small response from anywhere else."""
        f = Fetcher(cache_dir=self.tmp)
        self.assertFalse(f._looks_blocked("https://en.wikipedia.org/x", "{}"))


class TestRetryAndBackoff(FetcherTestCase):
    def test_retries_through_a_block_and_returns_the_real_page(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE, HOLDING_PAGE, REAL_PAGE])
        html = f.get(EVENT_URL)
        self.assertIn("b-content__title", html)
        self.assertEqual(f.session.calls, 3)

    def test_backoff_grows(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE, HOLDING_PAGE, REAL_PAGE])
        f.get(EVENT_URL)
        waits = [s for s in self.slept if s >= config.BLOCK_BACKOFF_SEC]
        self.assertGreaterEqual(len(waits), 2)
        self.assertGreater(waits[1], waits[0])

    def test_being_blocked_slows_the_whole_run_down(self):
        """Getting throttled is a reason to ask less often from then on."""
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE, REAL_PAGE])
        before = f.interval
        f.get(EVENT_URL)
        self.assertGreater(f.interval, before)
        self.assertLessEqual(f.interval, config.MAX_INTERVAL_SEC)

    def test_gives_up_with_a_clear_error(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE] * 10)
        with self.assertRaises(BlockedError) as caught:
            f.get(EVENT_URL)
        self.assertIn("rate-limiting", str(caught.exception))

    def test_a_holding_page_is_never_cached(self):
        """Caching a block would poison every later run for that URL."""
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE] * 10)
        with self.assertRaises(BlockedError):
            f.get(EVENT_URL)
        body, _ = f._paths(EVENT_URL)
        self.assertFalse(body.exists())

    def test_good_page_is_cached_and_reused(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([REAL_PAGE])
        f.get(EVENT_URL, immutable=True)
        f.session = FakeSession([])
        f.session.calls = 0
        f.get(EVENT_URL, immutable=True)
        self.assertEqual(f.session.calls, 0, "an immutable page must not refetch")


class TestGivingUp(FetcherTestCase):
    """Once the site is refusing every page, retrying is not resilience — it is
    an hour of hammering someone else's server for decorative data."""

    def test_trips_after_a_streak_and_then_fails_fast(self):
        from fetcher import HostRefusing
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE] * 500)

        for i in range(config.BLOCK_GIVE_UP_STREAK - 1):
            with self.assertRaises(BlockedError):
                f.get(f"http://ufcstats.com/fighter-details/{i}")
            self.assertFalse(f.giving_up)

        with self.assertRaises(HostRefusing):
            f.get("http://ufcstats.com/fighter-details/last")
        self.assertTrue(f.giving_up)

        calls_before = f.session.calls
        with self.assertRaises(HostRefusing):
            f.get("http://ufcstats.com/fighter-details/another")
        self.assertEqual(f.session.calls, calls_before,
                         "once we have given up, do not make the request at all")

    def test_a_good_page_resets_the_streak(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([HOLDING_PAGE] * 4 + [REAL_PAGE])
        with self.assertRaises(BlockedError):
            f.get("http://ufcstats.com/event-details/one")
        self.assertEqual(f.block_streak, 1)
        f.session = FakeSession([REAL_PAGE])
        f.get("http://ufcstats.com/event-details/two")
        self.assertEqual(f.block_streak, 0)
        self.assertFalse(f.giving_up)


class TestCacheEncoding(FetcherTestCase):
    """An earlier build wrote the cache with the platform default encoding. On
    Windows that is cp1252, and reading those files back as UTF-8 dies on the
    first accented fighter name — taking the whole run with it."""

    ACCENTED = REAL_PAGE.replace("UFC 300", "José Aldo vs. Petr Yan")

    def test_reads_a_legacy_cp1252_cache(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        body, meta = f._paths(EVENT_URL)
        body.write_bytes(self.ACCENTED.encode("cp1252"))      # the old writer
        meta.write_text('{"immutable": true, "fetched_at": 99999999999}')
        f.session = FakeSession([])
        html = f.get(EVENT_URL, immutable=True)
        self.assertIn("José Aldo", html)
        self.assertEqual(f.session.calls, 0)

    def test_writes_utf8_regardless_of_platform(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        f.session = FakeSession([self.ACCENTED])
        f.get(EVENT_URL, immutable=True)
        body, _ = f._paths(EVENT_URL)
        self.assertIn("José Aldo", body.read_bytes().decode("utf-8"))

    def test_undecodable_cache_heals_itself(self):
        f = Fetcher(cache_dir=self.tmp, use_browser_fallback=False)
        body, meta = f._paths(EVENT_URL)
        body.write_bytes(b"\xff\xfe\x00\x00garbage" * 50)
        meta.write_text('{"immutable": true, "fetched_at": 99999999999}')
        f.session = FakeSession([REAL_PAGE])
        html = f.get(EVENT_URL, immutable=True)
        self.assertIn("b-content__title", html)
        self.assertEqual(f.session.calls, 1, "should refetch, not crash")


class TestRenderNeverRaises(FetcherTestCase):
    def test_render_returns_none_when_playwright_explodes(self):
        """A Playwright error must not escape into the pipeline — that is what
        killed a run at the very last step after 20 minutes of scraping."""
        f = Fetcher(cache_dir=self.tmp)

        class Boom:
            def new_page(self, **kw):
                raise RuntimeError("Page.content: page is navigating")

        f._browser = Boom()
        self.assertIsNone(f._render(EVENT_URL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
