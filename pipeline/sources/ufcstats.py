"""Parsers for ufcstats.com.

ufcstats is the system of record for *what happened*: who fought, who won, how,
in which round, and the per-fighter statistics for the bout. It is plain server
-rendered HTML with stable ids in its URLs, which makes it a good scrape target.

Every parser here fails loudly. A silent empty list from a scraper is how a
fantasy league quietly stops scoring for three weeks, so if the markup moves,
these raise ParseError with the URL and what was missing.
"""
from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

import config


class ParseError(RuntimeError):
    pass


ID_RE = re.compile(r"/(?:event|fight|fighter)-details/([0-9a-f]+)")
_WS = re.compile(r"\s+")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _txt(node) -> str:
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip() if node else ""


def _id_from(href: str) -> str | None:
    m = ID_RE.search(href or "")
    return m.group(1) if m else None


def _date(raw: str) -> str:
    """'April 13, 2024' -> '2024-04-13'."""
    raw = raw.replace("Date:", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ParseError(f"unparseable date: {raw!r}")


def _int(raw: str, default: int = 0) -> int:
    m = re.search(r"-?\d+", raw or "")
    return int(m.group()) if m else default


def _of(raw: str) -> tuple[int, int]:
    """'104 of 187' -> (104, 187)."""
    m = re.search(r"(\d+)\s+of\s+(\d+)", raw or "")
    return (int(m.group(1)), int(m.group(2))) if m else (_int(raw), 0)


def _clock(raw: str) -> int:
    """'3:12' -> 192 seconds. '--' -> 0."""
    m = re.match(r"\s*(\d+):(\d{1,2})\s*$", raw or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


# --------------------------------------------------------------- events index
def parse_events_index(html: str, *, url: str = config.EVENTS_COMPLETED) -> list[dict]:
    """Rows of the completed- or upcoming-events table."""
    soup = _soup(html)
    table = soup.select_one("table.b-statistics__table-events")
    if table is None:
        raise ParseError(f"{url}: no table.b-statistics__table-events")

    events = []
    for row in table.select("tr.b-statistics__table-row"):
        link = row.select_one("a.b-link")
        if link is None:                     # header row
            continue
        event_id = _id_from(link.get("href", ""))
        if not event_id:
            raise ParseError(f"{url}: event link with no id: {link.get('href')!r}")
        date_node = row.select_one("span.b-statistics__date")
        cols = row.select("td.b-statistics__table-col")
        events.append({
            "event_id": event_id,
            "url": link["href"].strip(),
            "name": _txt(link),
            "date": _date(_txt(date_node)) if date_node else None,
            "location": _txt(cols[-1]) if len(cols) > 1 else "",
        })

    if not events:
        raise ParseError(f"{url}: events table parsed to zero rows")
    return events


# ---------------------------------------------------------------- event page
def parse_event(html: str, *, url: str = "") -> dict:
    """One event: its metadata plus a summary row for every bout on the card."""
    soup = _soup(html)
    title = soup.select_one("h2.b-content__title span.b-content__title-highlight") \
        or soup.select_one("h2.b-content__title")
    if title is None:
        # Say what actually came back. "No event title" on a page that turned out
        # to be a 400-byte bot check wastes everybody's time.
        head = _WS.sub(" ", soup.get_text(" ", strip=True))[:160]
        raise ParseError(f"{url}: no event title in {len(html)} bytes. "
                         f"Page began: {head!r}")

    info = {"date": None, "location": ""}
    for li in soup.select("ul.b-list__box-list li.b-list__box-list-item"):
        label = _txt(li.select_one("i"))
        value = _txt(li).replace(label, "", 1).strip()
        if label.startswith("Date"):
            info["date"] = _date(value)
        elif label.startswith("Location"):
            info["location"] = value

    bouts = []
    table = soup.select_one("table.b-fight-details__table")
    rows = table.select("tr.b-fight-details__table-row") if table else []
    for row in rows:
        link = row.get("data-link") or ""
        bout_id = _id_from(link)
        if not bout_id:                       # header row
            continue
        cols = row.select("td.b-fight-details__table-col")
        if len(cols) < 10:
            raise ParseError(f"{url}: bout row has {len(cols)} columns, expected 10")

        names = cols[1].select("a.b-link")
        if len(names) != 2:
            raise ParseError(f"{url}: bout {bout_id} has {len(names)} fighter links")
        fighters = [{"fighter_id": _id_from(a.get("href", "")), "name": _txt(a)} for a in names]

        flag = _txt(cols[0]).lower()
        outcome = "win" if "win" in flag else ("draw" if "draw" in flag else
                                               ("nc" if flag else "unknown"))

        weight_text = _txt(cols[6])
        method = _txt(cols[7].select_one("p")) if cols[7].select_one("p") else _txt(cols[7])

        bouts.append({
            "bout_id": bout_id,
            "url": link.strip(),
            "fighters": fighters,
            # ufcstats always lists the winner first
            "winner_id": fighters[0]["fighter_id"] if outcome == "win" else None,
            "outcome": outcome,
            "weight_class": _weight_class(weight_text),
            "title_bout": "title" in weight_text.lower(),
            "method": _method(method),
            "round": _int(_txt(cols[8])),
            "time": _txt(cols[9]),
        })

    return {
        "event_id": _id_from(url) or "",
        "name": _txt(title),
        "date": info["date"],
        "location": info["location"],
        "bouts": bouts,
    }


# Longest first, always: "Light Heavyweight" contains "Heavyweight" and
# "Women's Bantamweight" contains "Bantamweight", so a shortest-first scan
# silently files half the roster in the wrong division.
_DIVISIONS_BY_LENGTH = sorted(config.DIVISIONS, key=len, reverse=True)


def _weight_class(raw: str) -> str:
    raw = raw.replace("Bout", "").replace("Title", "").strip()
    lowered = raw.lower()
    for name in _DIVISIONS_BY_LENGTH:
        if name.lower() in lowered:
            return name
    return raw or "Unknown"


def _method(raw: str) -> str:
    r = (raw or "").upper()
    if "KO" in r or "TKO" in r:
        return "KO/TKO"
    if "SUB" in r:
        return "SUB"
    if "DEC" in r:
        return "DEC"
    if "DQ" in r:
        return "DQ"
    if "OVERTURNED" in r or "NC" in r:
        return "NC"
    return raw.strip() or "UNKNOWN"


# ----------------------------------------------------------------- fight page
_BONUS_IMG = re.compile(r"/(belt|perf|fight|ko|sub)\.png", re.I)
_BONUS_MAP = {"perf": "performance", "fight": "fight_of_night",
              "ko": "ko_of_night", "sub": "sub_of_night"}

# Column order of the "Totals" table on a fight-details page.
_TOTALS = ["fighter", "kd", "sig_str", "sig_pct", "total_str", "td",
           "td_pct", "sub_att", "rev", "ctrl"]


def parse_fight(html: str, *, url: str = "") -> dict:
    """One bout: result, bonuses, and the per-fighter statistics."""
    soup = _soup(html)
    persons = soup.select("div.b-fight-details__person")
    if len(persons) != 2:
        raise ParseError(f"{url}: found {len(persons)} fighters, expected 2")

    fighters, winner_id, outcome = [], None, "unknown"
    for p in persons:
        link = p.select_one("a.b-link") or p.select_one("h3 a")
        fid = _id_from(link.get("href", "")) if link else None
        status = _txt(p.select_one("i.b-fight-details__person-status")).upper()
        fighters.append({"fighter_id": fid, "name": _txt(link), "status": status})
        if status == "W":
            winner_id, outcome = fid, "win"
    if outcome != "win":
        statuses = {f["status"] for f in fighters}
        outcome = "draw" if "D" in statuses else ("nc" if "NC" in statuses else "unknown")

    title_node = soup.select_one("i.b-fight-details__fight-title") \
        or soup.select_one("div.b-fight-details__fight-head")
    title_text = _txt(title_node)
    bonuses, has_belt = [], False
    for img in (title_node.select("img") if title_node else []):
        m = _BONUS_IMG.search(img.get("src", ""))
        if not m:
            continue
        kind = m.group(1).lower()
        if kind == "belt":
            has_belt = True
        elif kind in _BONUS_MAP:
            bonuses.append(_BONUS_MAP[kind])

    details = {}
    for item in soup.select("i.b-fight-details__text-item, i.b-fight-details__text-item_first"):
        label = _txt(item.select_one("i.b-fight-details__label"))
        if not label:
            continue
        details[label.rstrip(":").lower()] = _txt(item).replace(label, "", 1).strip()

    result = {
        "bout_id": _id_from(url) or "",
        "fighters": fighters,
        "winner_id": winner_id,
        "outcome": outcome,
        "title_bout": ("title" in title_text.lower()) or has_belt,
        "weight_class": _weight_class(title_text),
        "method": _method(details.get("method", "")),
        "method_detail": details.get("method", ""),
        "round": _int(details.get("round", "")),
        "time": details.get("time", ""),
        "time_format": details.get("time format", ""),
        "referee": details.get("referee", ""),
        "bonuses": bonuses,
        "stats": {},
    }

    totals = _find_totals_table(soup)
    if totals is None:
        # No stats is normal for very old cards; a result with no stats is still
        # scoreable in outcome mode.
        return result

    cells = totals.select("td.b-fight-details__table-col")
    if len(cells) < len(_TOTALS):
        raise ParseError(f"{url}: totals table has {len(cells)} cells")
    by_col = {}
    for name, cell in zip(_TOTALS, cells):
        by_col[name] = [_txt(p) for p in cell.select("p.b-fight-details__table-text")]

    for i, f in enumerate(fighters):
        if not f["fighter_id"]:
            continue
        def col(name, idx=i):
            vals = by_col.get(name, [])
            return vals[idx] if idx < len(vals) else ""
        sig_l, sig_a = _of(col("sig_str"))
        td_l, td_a = _of(col("td"))
        _, tot_a = _of(col("total_str"))
        result["stats"][f["fighter_id"]] = {
            "kd": _int(col("kd")),
            "sig_str_landed": sig_l,
            "sig_str_attempted": sig_a,
            "total_str_attempted": tot_a,
            "td_landed": td_l,
            "td_attempted": td_a,
            "sub_att": _int(col("sub_att")),
            "rev": _int(col("rev")),
            "ctrl_sec": _clock(col("ctrl")),
        }
    return result


def _find_totals_table(soup):
    """Return the single data row of the bout-totals table.

    A fight-details page carries several stats tables: bout totals, the same
    numbers per round, then the significant-strike breakdown and its per-round
    version. Rather than depend on the surrounding labels (which sit in sibling
    sections and have moved before), key on the shape: the totals table is the
    first one with exactly one data row — the per-round tables have one row per
    round.
    """
    for table in soup.select("table.b-fight-details__table"):
        body = table.select_one("tbody") or table
        rows = [r for r in body.select("tr") if r.select("td")]
        if len(rows) == 1:
            return rows[0]
    table = soup.select_one("table.b-fight-details__table")
    if table is None:
        return None
    rows = [r for r in table.select("tr") if r.select("td")]
    return rows[0] if rows else None


# --------------------------------------------------------------- fighter page
def parse_fighter(html: str, *, url: str = "") -> dict:
    soup = _soup(html)
    name = _txt(soup.select_one("span.b-content__title-highlight"))
    record = _txt(soup.select_one("span.b-content__title-record"))
    if not name:
        raise ParseError(f"{url}: no fighter name")
    m = re.search(r"(\d+)-(\d+)-(\d+)", record)
    nickname = _txt(soup.select_one("p.b-content__Nickname"))
    attrs = {}
    for li in soup.select("li.b-list__box-list-item"):
        label = _txt(li.select_one("i"))
        if label:
            attrs[label.rstrip(":").lower()] = _txt(li).replace(label, "", 1).strip()
    return {
        "fighter_id": _id_from(url) or "",
        "name": name,
        "nickname": nickname.strip('"') if nickname else "",
        "wins": int(m.group(1)) if m else 0,
        "losses": int(m.group(2)) if m else 0,
        "draws": int(m.group(3)) if m else 0,
        "height": attrs.get("height", ""),
        "reach": attrs.get("reach", ""),
        "stance": attrs.get("stance", ""),
        "dob": attrs.get("dob", ""),
    }
