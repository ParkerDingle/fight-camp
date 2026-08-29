"""Configuration for the UFC fantasy data pipeline.

Everything that might reasonably need changing lives here so the rest of the
code has no magic strings in it.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("UFC_DATA_DIR", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "ufc.db"
EXPORT_PATH = DATA_DIR / "league_data.json"
# The built page, read back for its live scoring values when composing alerts.
APP_BUILD = ROOT / "dist" / "octagon-draft.html"
LOG_PATH = DATA_DIR / "pipeline.log"

# --- sources -----------------------------------------------------------------
UFCSTATS_BASE = "http://ufcstats.com"
EVENTS_COMPLETED = UFCSTATS_BASE + "/statistics/events/completed?page=all"
EVENTS_UPCOMING = UFCSTATS_BASE + "/statistics/events/upcoming?page=all"

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_RANKINGS_PAGE = "UFC_rankings"
WIKI_EVENTS_PAGE = "List_of_UFC_events"
# The maintained list of everyone currently under contract. This is the only
# source that knows about a fighter who has been signed but has not fought yet,
# or one who was released last week and can no longer score.
WIKI_ROSTER_PAGE = "List_of_current_UFC_fighters"

# Identify yourself. Scrapers that look anonymous get blocked; ones that leave a
# contact address generally do not. Put a real address here before running.
CONTACT = os.environ.get("UFC_SCRAPER_CONTACT", "you@example.com")
USER_AGENT = f"octagon-draft-fantasy/1.0 (+{CONTACT})"

# --- politeness --------------------------------------------------------------
# If ufcstats challenges every request from your network, set UFC_FORCE_BROWSER=1
# to render pages in Chromium from the start instead of paying a failed plain
# request first. Slower per page, much faster over a 500-page backfill.
FORCE_BROWSER = os.environ.get("UFC_FORCE_BROWSER", "").lower() in ("1", "true", "yes")

MIN_INTERVAL_SEC = float(os.environ.get("UFC_MIN_INTERVAL", 1.5))
MAX_INTERVAL_SEC = 10.0     # ceiling once we start getting throttled
MAX_RETRIES = 4             # transport-level retries (timeouts, 5xx)
BLOCK_RETRIES = 4           # retries when the host serves a holding page
BLOCK_BACKOFF_SEC = 6.0     # first wait after a holding page; doubles each try
# After this many pages in a row that were blocked all the way through their
# retries, stop asking. At that point the site is not rate-limiting a burst, it
# is refusing this machine, and another hundred requests will not change that.
BLOCK_GIVE_UP_STREAK = 4
BACKOFF_BASE_SEC = 2.0
REQUEST_TIMEOUT = 25
CACHE_TTL_SEC = 60 * 60 * 12   # re-validate pages older than this

# --- domain ------------------------------------------------------------------
DIVISIONS = {
    "Heavyweight": "HW",
    "Light Heavyweight": "LHW",
    "Middleweight": "MW",
    "Welterweight": "WW",
    "Lightweight": "LW",
    "Featherweight": "FW",
    "Bantamweight": "BW",
    "Flyweight": "FLW",
    "Women's Bantamweight": "WBW",
    "Women's Featherweight": "WFW",
    "Women's Flyweight": "WFLW",
    "Women's Strawweight": "WSW",
    "Catch Weight": "CW",
    "Open Weight": "OW",
}

# A bout is only scored if it is a real UFC bout in one of these divisions.
SCORABLE_DIVISIONS = [d for d in DIVISIONS if d not in ("Catch Weight", "Open Weight")]

# How long after a card's start time the post-event run should fire.
POST_EVENT_DELAY_HOURS = 6
