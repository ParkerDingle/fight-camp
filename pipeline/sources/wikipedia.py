"""Wikipedia source, via the MediaWiki API.

ufcstats tells us what already happened. It does not tell us what is *booked* —
and a fantasy manager needs to see that their fighter is on next week's card.
Wikipedia is the best free source for announced bouts, and it also carries two
things ufcstats does not reliably expose: official rankings, and weigh-in notes
(missed weight, catchweight).

We use the API rather than scraping article HTML off the rendered page, because
the API is the supported interface, is rate-limit friendly, and returns clean
HTML we can parse without fighting the site chrome.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

import config

log = logging.getLogger("wikipedia")

_WS = re.compile(r"\s+")
_CITE = re.compile(r"\[\d+\]")


def _txt(node) -> str:
    if node is None:
        return ""
    return _CITE.sub("", _WS.sub(" ", node.get_text(" ", strip=True))).strip()


def page_html(fetcher, page: str) -> str:
    """Rendered HTML for one article, through the API."""
    data = fetcher.get_json(config.WIKI_API, {
        "action": "parse", "page": page, "prop": "text",
        "format": "json", "formatversion": "2", "redirects": "1",
    })
    if "error" in data:
        raise RuntimeError(f"wikipedia: {data['error'].get('info')}")
    return data["parse"]["text"]


# ------------------------------------------------------------------ schedule
def parse_scheduled_events(html: str) -> list[dict]:
    """The 'Scheduled events' table on List_of_UFC_events."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.select("table.wikitable"):
        headers = [_txt(th).lower() for th in table.select("tr th")][:6]
        if not any("event" in h for h in headers):
            continue
        for row in table.select("tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            name = _txt(cells[0])
            date = _txt(cells[1])
            if not name or not re.search(r"\d{4}", date):
                continue
            link = cells[0].select_one("a")
            out.append({
                "name": name,
                "date_text": date,
                "venue": _txt(cells[2]) if len(cells) > 2 else "",
                "location": _txt(cells[3]) if len(cells) > 3 else "",
                "page": link.get("title") if link else name,
            })
        if out:
            break
    return out


# ----------------------------------------------------------------- fight card
_VS = re.compile(r"\s+vs\.?\s+", re.I)


def parse_event_card(html: str) -> dict:
    """Announced bouts and weigh-in notes from a single event article.

    Handles both shapes Wikipedia uses: the results table on a past event
    (`A | def. | B | Method | Round | Time | Notes`) and the announced-bout
    listing on an upcoming one (`Weight class | A vs. B`).
    """
    soup = BeautifulSoup(html, "lxml")
    bouts, notes = [], []

    for table in soup.select("table.wikitable"):
        headers = [_txt(th).lower() for th in table.select("tr th")]
        if not headers or not any("weight class" in h for h in headers):
            continue
        section = _section_for(table)
        for row in table.select("tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            weight = _weight_class(_txt(cells[0]))
            joined = " ".join(_txt(c) for c in cells[1:])

            if len(cells) >= 3 and _txt(cells[2]).lower().startswith("def"):
                a, b = _txt(cells[1]), _txt(cells[3]) if len(cells) > 3 else ""
                bouts.append({
                    "weight_class": weight, "section": section,
                    "fighters": [a, b], "winner": a, "status": "completed",
                    "method": _txt(cells[4]) if len(cells) > 4 else "",
                    "notes": _txt(cells[-1]),
                })
            elif _VS.search(joined):
                a, b = [p.strip(" .;–—") for p in _VS.split(joined, 1)][:2]
                bouts.append({
                    "weight_class": weight, "section": section,
                    "fighters": [a, b], "winner": None, "status": "announced",
                    "method": "", "notes": "",
                })

    text = _txt(soup)
    for m in re.finditer(r"([A-Z][\w'’.\- ]{2,40}?)\s+(?:missed weight|came in .*?over|weighed in at [\d.]+ ?(?:lb|pounds).{0,40}?limit)", text):
        notes.append({"fighter": m.group(1).strip(), "type": "missed_weight"})

    bonuses = _parse_bonuses(soup)
    return {"bouts": bouts, "weigh_in_notes": notes, "bonuses": bonuses}


def _section_for(table) -> str:
    head = table.find_previous(["h2", "h3", "h4"])
    return _txt(head) if head else ""


def _parse_bonuses(soup) -> list[dict]:
    """The 'Bonus awards' section lists the $50,000 awards for the card."""
    out = []
    head = None
    for h in soup.select("h2, h3"):
        if "bonus" in _txt(h).lower():
            head = h
            break
    if head is None:
        return out
    for li in (head.find_next("ul").select("li") if head.find_next("ul") else []):
        line = _txt(li)
        kind = ("fight_of_night" if "fight of the night" in line.lower() else
                "performance" if "performance" in line.lower() else None)
        if not kind:
            continue
        names = re.split(r"\s+(?:and|vs\.?|&)\s+", line.split(":", 1)[-1])
        for n in names:
            n = re.sub(r"\(.*?\)", "", n).strip(" .;")
            if n:
                out.append({"fighter": n, "type": kind})
    return out


# Longest first — see the note in sources/ufcstats.py.
_DIVISIONS_BY_LENGTH = sorted(config.DIVISIONS, key=len, reverse=True)


def _weight_class(raw: str) -> str:
    raw = raw.replace("Bout", "").strip()
    lowered = raw.lower()
    for name in _DIVISIONS_BY_LENGTH:
        if name.lower() in lowered:
            return name
    return raw or "Unknown"


# ------------------------------------------------------------------ rankings
def _rank_token(raw: str) -> float | None:
    """'C' -> 0, 'IC' -> 0.5, '3' -> 3. Header cells and 'NR' -> None."""
    r = raw.strip().lower().strip("*†‡#. ")
    if r in ("c", "champion", "champ"):
        return 0.0
    if r in ("ic", "interim", "interim champion"):
        return 0.5
    m = re.match(r"^(\d{1,2})$", r)
    return float(m.group(1)) if m else None


def _clean_fighter(raw: str) -> str:
    return re.sub(r"\(.*?\)", "", raw).strip(" .;*†‡")


_RECORD = re.compile(r"^[\d–\-—]+$")


def _fighter_column(table) -> int | None:
    """Find the Fighter column by its header, not by position.

    These tables are not `Rank | Fighter`. They are
    `Rank | ISO | Fighter | Record | M | Win streak | …`, where ISO is a flag
    cell that renders as empty text — so taking the cell next to the rank gets
    you an empty string and the whole division silently drops out.
    """
    for row in table.find_all("tr"):
        for i, cell in enumerate(row.find_all(["th", "td"], recursive=False)):
            if _txt(cell).strip().lower() == "fighter":
                return i
    return None


def parse_rankings(html: str) -> dict[str, list[str]]:
    """division -> [champion, interim champion, #1, #2, ...].

    Two shapes have to survive here: the rank lives in a row-header `<th>`
    rather than a `<td>`, and the fighter's name is not the cell beside it.
    Both are read by content rather than position.

    The article carries the same divisions twice (meta rankings, then media
    rankings); the first block per division wins.

    Cosmetic only: rankings order the draft board and are never scoring input,
    so callers treat failure here as non-fatal.
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, list[str]] = {}
    for table in soup.find_all("table"):
        head = _txt(table.find_previous(["h2", "h3", "h4"]))
        division = _weight_class(head)
        if division not in config.DIVISIONS:
            continue
        name_col = _fighter_column(table)
        ranked: list[tuple[float, str]] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            rank = _rank_token(_txt(cells[0]))
            if rank is None:
                continue
            name = ""
            if name_col is not None and name_col < len(cells):
                name = _clean_fighter(_txt(cells[name_col]))
            if not name:
                # Fallback: first cell after the rank that reads like a name
                # rather than a flag (empty) or a record ("15–3").
                for cell in cells[1:]:
                    candidate = _clean_fighter(_txt(cell))
                    if candidate and not _RECORD.match(candidate):
                        name = candidate
                        break
            if name:
                ranked.append((rank, name))
        if ranked:
            ranked.sort(key=lambda pair: pair[0])
            out.setdefault(division, [name for _, name in ranked])
    return out


# ------------------------------------------------------------------- roster
def _heading_key(node) -> str:
    return _txt(node).lower().replace("[edit]", "").strip()


def _column_named(table, *labels) -> int | None:
    """Index of the first column whose header is one of `labels`.

    Same reasoning as `_fighter_column`: these tables lead with a flag cell that
    renders as empty text, so counting from the left finds nothing.
    """
    wanted = {l.lower() for l in labels}
    for row in table.find_all("tr"):
        for i, cell in enumerate(row.find_all(["th", "td"], recursive=False)):
            if _txt(cell).strip().lower() in wanted:
                return i
    return None


def parse_roster(html: str) -> dict[str, list[str]]:
    """division -> [names] of every fighter currently under contract.

    ufcstats knows who has fought; it has no concept of a roster. So a fighter
    signed last month and not yet booked is invisible to it, and one released
    yesterday looks exactly like one between camps. This article is the list
    that actually answers "is this person still in the UFC", and it is kept
    current by people who care about it being current.

    Only the tables under "Debuted fighters" count. The same article also
    carries recent signings, releases and suspensions in their own tables, and
    those would otherwise be read as divisions.
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, list[str]] = {}
    for table in soup.find_all("table"):
        top = table.find_previous("h2")
        # Equality, not "contains": the same article has a section called
        # "Debuted fighters' countries of origin", whose table is a tally of
        # nationalities and would otherwise be read as a division.
        if not top or _heading_key(top) != "debuted fighters":
            continue
        division = _weight_class(_txt(table.find_previous(["h3", "h2"])))
        if division not in config.SCORABLE_DIVISIONS:
            continue
        col = _column_named(table, "name", "fighter")
        if col is None:
            log.warning("roster: %s table has no Name column", division)
            continue
        seen = out.setdefault(division, [])
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) <= col or cells[col].name == "th":
                continue
            name = _clean_fighter(_txt(cells[col]))
            if not name or _RECORD.match(name):
                continue
            if name not in seen:
                seen.append(name)
    return {d: n for d, n in out.items() if n}
