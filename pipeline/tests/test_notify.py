"""Notifier tests. No network: the channels are stubbed."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify   # noqa: E402

PERFS = [
    {"name": "Justin Gaethje", "points": 317.8, "won": True, "opponent": "Ilia Topuria"},
    {"name": "Ilia Topuria", "points": 37.8, "won": False, "opponent": "Justin Gaethje"},
]


class TestConfigured(unittest.TestCase):
    def setUp(self):
        self.saved = {k: v for k, v in os.environ.items() if k.startswith("UFC_")}
        for k in list(os.environ):
            if k.startswith("UFC_"):
                del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("UFC_"):
                del os.environ[k]
        os.environ.update(self.saved)

    def test_nothing_configured(self):
        self.assertEqual(notify.configured(), [])

    def test_each_channel_switches_itself_on(self):
        os.environ["UFC_NTFY_TOPIC"] = "the-fight-camp-8fj2"
        self.assertEqual(notify.configured(), ["ntfy"])
        os.environ["UFC_DISCORD_WEBHOOK"] = "https://discord.com/api/webhooks/x"
        self.assertEqual(notify.configured(), ["ntfy", "discord"])

    def test_telegram_needs_both_halves(self):
        os.environ["UFC_TELEGRAM_TOKEN"] = "123:ABC"
        self.assertEqual(notify.configured(), [])
        os.environ["UFC_TELEGRAM_CHAT"] = "-100123"
        self.assertEqual(notify.configured(), ["telegram"])

    def test_a_failing_channel_does_not_stop_the_others(self):
        os.environ["UFC_NTFY_TOPIC"] = "t"
        os.environ["UFC_DISCORD_WEBHOOK"] = "https://discord.com/api/webhooks/x"
        calls = []
        notify._CHANNELS["ntfy"] = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
        notify._CHANNELS["discord"] = lambda *a: calls.append("discord")
        try:
            self.assertEqual(notify.send("t", "b"), ["discord"])
            self.assertEqual(calls, ["discord"])
        finally:
            notify._CHANNELS["ntfy"] = notify._ntfy
            notify._CHANNELS["discord"] = notify._discord


class TestMessage(unittest.TestCase):
    def test_reads_like_something_worth_unlocking_a_phone_for(self):
        title, body = notify.event_summary("UFC Freedom 250", "2026-06-14", PERFS)
        self.assertEqual(title, "UFC Freedom 250 scored")
        self.assertIn("1. Justin Gaethje — 318 pts (def. Ilia Topuria)", body)
        self.assertIn("2. Ilia Topuria — 38 pts (lost to Justin Gaethje)", body)
        self.assertIn("Rebuild and republish", body)

    def test_limit_is_respected(self):
        many = PERFS * 6
        _, body = notify.event_summary("UFC 300", "2026-01-01", many, limit=3)
        self.assertEqual(body.count(" pts ("), 3)

    def test_empty_card_says_so_instead_of_lying(self):
        _, body = notify.event_summary("UFC 300", "2026-01-01", [])
        self.assertIn("No scored bouts", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
