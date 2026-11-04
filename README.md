# The Fight Camp: a fantasy UFC league that keeps itself up to date

**Live league:** https://parkerdingle.github.io/fight-camp/

A fantasy league for five managers who draft UFC fighters and score points from real fight results. Behind the app is a Python data pipeline. It scrapes results, cleans them, stores them in SQLite, scores them, projects each fighter's season value, and republishes the site every night on GitHub Actions, so nobody has to run anything by hand.

```
ufcstats.com ─┐                                                    ┌─> league_data.json ─> app (PWA) ─> GitHub Pages
              ├─> fetcher ─> parsers ─> normalize ─> SQLite (ufc.db) ┤
Wikipedia ────┘   (cached,   (events,   (name        (system of     ├─> scoring + projections
                  throttled)  bouts,     matching,     record,       └─> notify (ntfy / Discord / Telegram / email)
                              stats)     rankings,     idempotent
                                         roster)       upserts)
```

## The data

| Table | Rows | Contents |
|---|---:|---|
| `fighters` | 763 | Record, division, current rank, whether they're under contract |
| `events` | 57 | Every card from Sept 2025 on, plus announced future cards |
| `bouts` | 615 | Winner, method, round, time, title fight, bonuses |
| `bout_stats` | 1,056 | Per-fighter significant strikes, takedowns, knockdowns, submission attempts, control time |
| `aliases` | 62 | Spellings that differ between sources, mapped to one fighter ID |
| `runs` | 87 | Audit log of every pipeline run |

## What makes it work

- **Idempotent pipeline.** Every write is an upsert keyed on the source's own stable ID, so a run that crashes halfway can just be run again. Re-scraping a card that hasn't changed does nothing.
- **Two jobs that don't trust each other.** `post-event` scores a card a few hours after it ends. `nightly` re-checks every past event that's missing results and picks up amended results, such as overturned decisions or failed drug tests.
- **Polite, block-aware scraping.** Requests go out one at a time and are cached with ETag/Last-Modified, so a nightly pass over the history comes back as hundreds of 304s rather than full page loads. When ufcstats rate-limits, it returns HTTP 200 with a ~385-byte holding page instead of a 429. The fetcher detects that page, treats it as a block rather than content, and backs off.
- **Entity resolution across sources.** Fighter names are normalized and matched across ufcstats and Wikipedia (rankings and the current roster), with an alias table for the cases that don't match.
- **Data-quality handling.** Bouts scraped before a parser fix held round-one totals instead of whole-fight totals, which undercounts a decision by about 70%. Those rows aren't used for projections. Instead, the stat component is modelled from fight duration, fitted only on bouts that were re-scraped correctly.
- **Scoring lives in one place.** The Python scorer reads its point values from the built app, so the notifier and the scoreboard can't drift apart. A test pins the two together.
- **139 tests** cover the parsers, fetcher, merge logic, scoring, projections and notifier.

## Season projections

`projections.py` estimates what each fighter is worth over the next 12 months, which is the number that matters on draft day:

```
expected season points = expected fights × expected points per fight
```

Both terms come from the last 12 months of real results run through the league's scoring engine. Both are **shrunk toward the divisional average**, with a prior worth two fights, because most fighters have only one or two bouts on record. With one bout, a fighter's estimate sits a third of the way from the average toward what they did. With six, it's three-quarters of the way. That stops a single lucky knockout from out-projecting a proven contender, and it means a champion who defends once a year can rank below a gatekeeper who fights three times.

## Automation

`.github/workflows/scores.yml` runs twice on Sunday after Saturday cards and once a day as a safety net. It scrapes, refreshes the roster and rankings, rebuilds the site, commits the updated database (the database is the system of record) and sends notifications. The scrape step has a hard time budget and is allowed to fail, so a day when ufcstats refuses GitHub's datacenter IPs can't stop the league from publishing.

## Run it locally

```bash
cd pipeline
pip install -r requirements.txt
python pipeline.py backfill --since 2025-09-01   # one-time history load
python pipeline.py nightly                       # the nightly job
python pipeline.py status                        # what's in the database
python -m pytest tests
```

## Stack

Python · SQLite · requests + BeautifulSoup/lxml · GitHub Actions · GitHub Pages · vanilla JS PWA
