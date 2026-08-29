"""Push a message to phones after a card is scored.

Four channels, all optional, all configured by environment variable in .env.
Whichever ones are configured get used; the rest are skipped silently. Nothing
here is ever allowed to fail a pipeline run — a missed notification is an
annoyance, a failed scrape is a league that stops scoring.

    UFC_NTFY_TOPIC=the-fight-camp-8fj2      ntfy.sh — no account, install the
                                            ntfy app, subscribe to the topic
    UFC_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
    UFC_TELEGRAM_TOKEN=123:ABC              from @BotFather
    UFC_TELEGRAM_CHAT=-1001234567890
    UFC_SMTP_HOST / _PORT / _USER / _PASS / _FROM / _TO

Pick a topic name nobody would guess: an ntfy topic is public to anyone who
knows it.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger("notify")
TIMEOUT = 15


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def configured() -> list[str]:
    """Which channels are switched on."""
    out = []
    if _env("UFC_NTFY_TOPIC"):
        out.append("ntfy")
    if _env("UFC_DISCORD_WEBHOOK"):
        out.append("discord")
    if _env("UFC_TELEGRAM_TOKEN") and _env("UFC_TELEGRAM_CHAT"):
        out.append("telegram")
    if _env("UFC_SMTP_HOST") and _env("UFC_SMTP_TO"):
        out.append("email")
    return out


# ------------------------------------------------------------------ channels
def _ntfy(title: str, body: str, link: str | None) -> None:
    server = _env("UFC_NTFY_SERVER") or "https://ntfy.sh"
    headers = {"Title": title, "Tags": "boxing", "Priority": "default"}
    if link:
        headers["Click"] = link
    requests.post(f"{server.rstrip('/')}/{_env('UFC_NTFY_TOPIC')}",
                  data=body.encode("utf-8"), headers=headers, timeout=TIMEOUT
                  ).raise_for_status()


def _discord(title: str, body: str, link: str | None) -> None:
    content = f"**{title}**\n{body}" + (f"\n{link}" if link else "")
    requests.post(_env("UFC_DISCORD_WEBHOOK"), json={"content": content[:1900]},
                  timeout=TIMEOUT).raise_for_status()


def _telegram(title: str, body: str, link: str | None) -> None:
    text = f"*{title}*\n{body}" + (f"\n{link}" if link else "")
    requests.post(
        f"https://api.telegram.org/bot{_env('UFC_TELEGRAM_TOKEN')}/sendMessage",
        json={"chat_id": _env("UFC_TELEGRAM_CHAT"), "text": text[:3900],
              "parse_mode": "Markdown", "disable_web_page_preview": True},
        timeout=TIMEOUT).raise_for_status()


def _email(title: str, body: str, link: str | None) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = _env("UFC_SMTP_FROM") or _env("UFC_SMTP_USER")
    msg["To"] = _env("UFC_SMTP_TO")
    msg.set_content(body + (f"\n\n{link}" if link else ""))
    port = int(_env("UFC_SMTP_PORT") or 587)
    with smtplib.SMTP(_env("UFC_SMTP_HOST"), port, timeout=TIMEOUT) as smtp:
        smtp.starttls()
        if _env("UFC_SMTP_USER"):
            smtp.login(_env("UFC_SMTP_USER"), _env("UFC_SMTP_PASS"))
        smtp.send_message(msg)


_CHANNELS = {"ntfy": _ntfy, "discord": _discord, "telegram": _telegram, "email": _email}


def send(title: str, body: str, link: str | None = None) -> list[str]:
    """Deliver to every configured channel. Returns the ones that succeeded."""
    delivered = []
    for name in configured():
        try:
            _CHANNELS[name](title, body, link or _env("UFC_LEAGUE_URL") or None)
            delivered.append(name)
        except Exception as exc:
            log.warning("%s notification failed: %s", name, exc)
    if not delivered:
        log.info("no notification channels configured (or all failed)")
    return delivered


# ------------------------------------------------------------------ messages
def event_summary(event_name: str, event_date: str, performances: list[dict],
                  limit: int = 5) -> tuple[str, str]:
    """Compose the after-the-card message.

    It reports the best performances on the card rather than league standings,
    because the pipeline does not know who drafted whom — rosters live in the
    published page, not in this database. Saying "Gaethje put up 318" is true
    and useful; inventing a standings table would not be.
    """
    title = f"{event_name} scored"
    lines = [event_date, ""]
    for i, p in enumerate(performances[:limit], 1):
        verb = "def." if p["won"] else "lost to"
        lines.append(f"{i}. {p['name']} — {p['points']:.0f} pts ({verb} {p['opponent']})")
    if not performances:
        lines.append("No scored bouts found on this card.")
    lines += ["", "Rebuild and republish to update the league."]
    return title, "\n".join(lines)
