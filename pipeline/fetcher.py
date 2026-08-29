"""Polite, cached, throttle-aware HTTP fetching.

Design notes:
  * One request at a time, with a floor between requests. Nothing here is worth
    hammering a free site for.
  * Responses are cached on disk and re-validated with ETag / Last-Modified, so
    a nightly run over 700 historical events costs ~700 conditional requests
    that all come back 304, not 700 full page loads.
  * A completed event never changes, so once a page is marked `immutable` in the
    cache it is served from disk forever without a network call.
  * ufcstats does not answer a rate limit with HTTP 429. It answers with 200 OK
    and a ~385-byte holding page that redirects with JavaScript. That is
    indistinguishable from a parse failure unless you look for it, so we look
    for it: a short body with none of the page furniture is treated as a block,
    not as content. When one appears we back off, slow the floor down for the
    rest of the run, and retry — because being throttled is a reason to ask
    less often, not a reason to give up.
  * Only if plain requests stay blocked do we spend a browser render on it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import requests

import config

log = logging.getLogger("fetcher")


class FetchError(RuntimeError):
    pass


class BlockedError(FetchError):
    """The host answered, but with a holding page instead of the content."""


class HostRefusing(BlockedError):
    """Enough pages in a row were blocked that we should stop asking entirely."""


# Interstitials that announce themselves.
_JS_MARKERS = (
    "This site requires JavaScript",
    "Checking your browser",
    "cf-browser-verification",
    "__cf_chl",
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "cf-mitigated",
    "Attention Required!",
)

# Markup that every real ufcstats page has. Its absence from a short response
# is what identifies the silent holding page.
_REAL_PAGE_MARKERS = ("b-content__title", "b-statistics__table", "b-fight-details",
                      "b-list__box-list", "<table")


def _looks_like_html(text: str) -> bool:
    head = text[:4000].lower()
    return any(tag in head for tag in ("<html", "<body", "<!doctype", "<table", "<div"))


class Fetcher:
    def __init__(self, cache_dir: Path | None = None, use_browser_fallback: bool = True):
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.use_browser_fallback = use_browser_fallback
        self.interval = config.MIN_INTERVAL_SEC
        self.blocks_seen = 0
        self.block_streak = 0        # consecutive pages blocked through all retries
        self.giving_up = False       # tripped once the streak says stop asking
        self._last_request = 0.0
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------ cache
    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.meta.json"

    def _read_meta(self, meta_path: Path) -> dict:
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_cached(self, body_path: Path) -> str | None:
        """Read a cached page, or None if it cannot be trusted.

        Earlier versions wrote the cache with the platform default encoding,
        which on Windows is cp1252. Reading those as UTF-8 raises on the first
        accented fighter name. Rather than force one encoding on files written
        by an older build, try both and treat anything still undecodable as a
        bad cache entry to be refetched — a corrupt cache should heal itself,
        not require someone to know to delete a directory.
        """
        try:
            raw = body_path.read_bytes()
        except OSError:
            return None
        for encoding in ("utf-8", "cp1252"):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            # cp1252 maps almost every byte to *something*, so a successful
            # decode proves nothing on its own. Require the result to look like
            # the HTML we cached, or a corrupt file comes back as mojibake and
            # gets parsed as a page.
            if _looks_like_html(text):
                return text
        return None

    def _discard(self, body_path: Path, meta_path: Path) -> None:
        for path in (body_path, meta_path):
            try:
                path.unlink()
            except OSError:
                pass

    def _store(self, url, body_path, meta_path, text, resp, immutable, rendered=False):
        # Always UTF-8, explicitly. The default is the platform's locale
        # encoding, which is how a cache written on Windows became unreadable.
        body_path.write_text(text, encoding="utf-8", errors="replace")
        meta_path.write_text(json.dumps({
            "url": url,
            "fetched_at": time.time(),
            "etag": resp.headers.get("ETag") if resp is not None else None,
            "last_modified": resp.headers.get("Last-Modified") if resp is not None else None,
            "immutable": bool(immutable),
            "rendered": rendered,
        }), encoding="utf-8")

    # -------------------------------------------------------------- blocking
    def _looks_blocked(self, url: str, text: str) -> bool:
        if any(m in text[:4000] for m in _JS_MARKERS):
            return True
        # The silent one: a short page with none of the site's real furniture.
        if "ufcstats.com" in url and len(text) < 1500:
            return not any(m in text for m in _REAL_PAGE_MARKERS)
        return False

    def _throttle_down(self) -> None:
        self.blocks_seen += 1
        before = self.interval
        self.interval = min(self.interval * 1.6, config.MAX_INTERVAL_SEC)
        if self.interval != before:
            log.info("slowing down: %.1fs -> %.1fs between requests", before, self.interval)

    # ------------------------------------------------------------------ fetch
    def get(self, url: str, *, immutable: bool = False, force: bool = False,
            render: bool = False) -> str:
        """Return page text, using the cache where allowed.

        immutable=True marks the page as never-changing (a finished event); it is
        then served from disk on every future run with no network call.
        render=True skips cache and plain request and uses a browser directly.
        """
        body_path, meta_path = self._paths(url)
        meta = self._read_meta(meta_path)

        if render:
            html = self._render(url)
            if not html:
                raise FetchError(
                    f"browser render requested for {url} but Chromium is unavailable.\n"
                    "    pip install playwright\n"
                    "    python -m playwright install chromium")
            self._store(url, body_path, meta_path, html, None, immutable, rendered=True)
            return html

        if body_path.exists() and not force:
            cached = self._read_cached(body_path)
            if cached is None:
                log.warning("unreadable cache entry for %s — refetching", url)
                self._discard(body_path, meta_path)
                meta = {}
            else:
                if meta.get("immutable"):
                    return cached
                if time.time() - meta.get("fetched_at", 0) < config.CACHE_TTL_SEC:
                    return cached

        headers = {}
        if body_path.exists() and not force:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        if self.giving_up:
            raise HostRefusing(
                f"not requesting {url}: the last {config.BLOCK_GIVE_UP_STREAK} pages were "
                "all blocked, so this run has stopped asking. Try again later.")

        blocked_text = None
        for attempt in range(config.BLOCK_RETRIES):
            resp = self._request(url, headers)

            if resp.status_code == 304 and body_path.exists():
                cached = self._read_cached(body_path)
                if cached is not None:
                    meta["fetched_at"] = time.time()
                    meta_path.write_text(json.dumps(meta), encoding="utf-8")
                    return cached
                # "Not modified" but we cannot read our copy: drop the
                # validators and ask for the page outright.
                self._discard(body_path, meta_path)
                headers = {}
                continue

            text = resp.text
            if not self._looks_blocked(url, text):
                self.block_streak = 0
                self._store(url, body_path, meta_path, text, resp, immutable)
                return text

            blocked_text = text
            self._throttle_down()
            wait = config.BLOCK_BACKOFF_SEC * (2 ** attempt)
            log.warning("holding page (%s bytes) for %s — backing off %.0fs "
                        "(attempt %s/%s)", len(text), url, wait,
                        attempt + 1, config.BLOCK_RETRIES)
            time.sleep(wait)

        # Plain requests stayed blocked. Now it is worth a browser.
        if self.use_browser_fallback:
            html = self._render(url)
            if html and not self._looks_blocked(url, html):
                self.block_streak = 0
                self._store(url, body_path, meta_path, html, None, immutable, rendered=True)
                return html

        self.block_streak += 1
        if self.block_streak >= config.BLOCK_GIVE_UP_STREAK:
            self.giving_up = True
            raise HostRefusing(
                f"{config.BLOCK_GIVE_UP_STREAK} pages in a row were blocked through "
                "every retry. The site is refusing this machine, not throttling a "
                "burst — stopping rather than hammering it. Whatever has already "
                "been collected is saved; try again in an hour or two.")

        raise BlockedError(
            f"{url}: still a {len(blocked_text or '')}-byte holding page after "
            f"{config.BLOCK_RETRIES} attempts and a browser render. "
            "The site is rate-limiting this machine; try again later, or raise "
            "UFC_MIN_INTERVAL.")

    def get_json(self, url: str, params: dict) -> dict:
        resp = self._request(url, {}, params=params)
        resp.raise_for_status()
        return resp.json()

    def _request(self, url: str, headers: dict, params: dict | None = None):
        last_exc = None
        for attempt in range(config.MAX_RETRIES):
            gap = time.time() - self._last_request
            if gap < self.interval:
                time.sleep(self.interval - gap)
            self._last_request = time.time()
            try:
                resp = self.session.get(
                    url, headers=headers, params=params,
                    timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("request failed (%s/%s) %s: %s",
                            attempt + 1, config.MAX_RETRIES, url, exc)
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if (retry_after or "").isdigit() \
                        else config.BACKOFF_BASE_SEC * (2 ** attempt)
                    if resp.status_code == 429:
                        self._throttle_down()
                    log.warning("HTTP %s for %s — backing off %.1fs",
                                resp.status_code, url, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400 and resp.status_code != 304:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                return resp
            time.sleep(config.BACKOFF_BASE_SEC * (2 ** attempt))
        raise FetchError(f"gave up on {url}: {last_exc}")

    # ----------------------------------------------------------------- browser
    def _render(self, url: str) -> str | None:
        """Render with Chromium. Never raises — returns None if it cannot.

        ufcstats' holding page navigates away as soon as its script runs, and
        asking for content mid-navigation throws. So: wait for the network to
        settle, then read with retries, and only accept a result that is big
        enough to be a real page.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        page = None
        try:
            if self._browser is None:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            page = self._browser.new_page(user_agent=config.USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            best = None
            for _ in range(4):
                try:
                    html = page.content()
                    if html and len(html) > 1500:
                        return html
                    best = html or best
                except Exception:
                    pass          # mid-navigation; wait and ask again
                page.wait_for_timeout(2000)
            return best
        except Exception as exc:
            log.warning("browser render failed for %s: %s", url, exc)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.session.close()
