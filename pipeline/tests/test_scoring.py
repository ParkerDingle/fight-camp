"""Pin the Python scoring against the app's own output.

The formula exists twice — once in the page's JavaScript, once in scoring.py —
because the notifier needs to compute points without a browser. Duplication like
that drifts silently unless something holds it in place, so these numbers were
read off the running app and are asserted here. If the app's formula changes and
this file is not updated, this test fails. That is the alarm working.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scoring   # noqa: E402

SC = scoring.DEFAULTS

GAETHJE = "9e8f6c728eb01124"

# UFC 324 — Gaethje def. Pimblett (#5) by decision in a 5-round title bout,
# Fight of the Night. The app renders this as 239.4.
PIMBLETT_BOUT = {
    "done": True, "winner_id": GAETHJE, "method": "DEC", "round": 5,
    "title": True, "perf": True,
    "stats": {GAETHJE: {"sig": 27, "td": 0, "kd": 1, "sub": 0, "ctrl": 61}},
}

# UFC Freedom 250 — Gaethje def. Topuria (#1) by KO in round 4 of a title bout,
# Performance of the Night. The app renders this as 317.8.
TOPURIA_BOUT = {
    "done": True, "winner_id": GAETHJE, "method": "KO/TKO", "round": 4,
    "title": True, "perf": True,
    "stats": {GAETHJE: {"sig": 32, "td": 0, "kd": 0, "sub": 0, "ctrl": 0}},
}


class TestParityWithTheApp(unittest.TestCase):
    def test_decision_win_over_a_top_five_opponent(self):
        got = scoring.bout_points(PIMBLETT_BOUT, GAETHJE, 5, SC)
        self.assertAlmostEqual(got["points"], 239.4, places=1)

    def test_knockout_win_over_the_number_one_contender(self):
        got = scoring.bout_points(TOPURIA_BOUT, GAETHJE, 1, SC)
        self.assertAlmostEqual(got["points"], 317.8, places=1)

    def test_the_breakdown_sums_to_the_total(self):
        for bout, rank in ((PIMBLETT_BOUT, 5), (TOPURIA_BOUT, 1)):
            got = scoring.bout_points(bout, GAETHJE, rank, SC)
            self.assertAlmostEqual(sum(v for _, v in got["lines"]), got["points"], places=1)


class TestOpponentQuality(unittest.TestCase):
    BASE = {"done": True, "winner_id": "me", "method": "DEC", "round": 3,
            "title": False, "perf": False, "stats": {}}

    def points(self, rank, winner="me"):
        return scoring.bout_points(dict(self.BASE, winner_id=winner), "me", rank, SC)["points"]

    def test_ladder_increases_with_opponent_rank(self):
        unranked, ranked, top10, top5, champ = (self.points(r) for r in (None, 12, 8, 3, 0))
        self.assertEqual(unranked, 75.0)                      # 25 + 50, no multiplier
        self.assertLess(unranked, ranked)
        self.assertLess(ranked, top10)
        self.assertLess(top10, top5)
        self.assertLess(top5, champ)
        self.assertAlmostEqual(champ, 150.0, places=1)        # doubled

    def test_a_losing_night_still_scales_up_while_it_stays_positive(self):
        """Appearance points mean most losses are still a net-positive night, so
        the multiplier applies normally: losing to the champion pays more than
        losing to an unranked fighter. That is the intended shape — taking the
        hard fight is worth something even when you lose it."""
        to_champ = self.points(0, winner="them")
        to_nobody = self.points(None, winner="them")
        self.assertAlmostEqual(to_nobody, 15.0, places=1)      # 25 appearance - 10
        self.assertAlmostEqual(to_champ, 30.0, places=1)       # doubled
        self.assertGreater(to_champ, to_nobody)

    def test_a_genuinely_bad_night_is_divided_instead(self):
        """When the night nets negative the multiplier inverts, so a mauling by
        the champion costs half what the same mauling by a nobody costs."""
        harsh = dict(SC, appear=0, statsOn=False)
        bout = dict(self.BASE, winner_id="them", method="KO/TKO")
        to_champ = scoring.bout_points(bout, "me", 0, harsh)["points"]
        to_nobody = scoring.bout_points(bout, "me", None, harsh)["points"]
        self.assertAlmostEqual(to_nobody, -20.0, places=1)
        self.assertAlmostEqual(to_champ, -10.0, places=1)
        self.assertGreater(to_champ, to_nobody)


class TestLoadingValuesFromTheBuiltPage(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        self.assertEqual(scoring.load_scoring("/nope/missing.html"), scoring.DEFAULTS)

    def test_reads_values_out_of_a_page(self):
        page = Path("/tmp/_scoring_page.html")
        page.write_text('<script id="league-state" type="application/json">'
                        '{"scoring":{"appear":40,"oppC":3}}</script>', encoding="utf-8")
        sc = scoring.load_scoring(page)
        self.assertEqual(sc["appear"], 40)
        self.assertEqual(sc["oppC"], 3)
        self.assertEqual(sc["winDec"], scoring.DEFAULTS["winDec"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
