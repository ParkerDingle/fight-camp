"""Pin the Python scoring against the app's own output.

The formula exists twice — once in the page's JavaScript, once in scoring.py —
because the notifier needs to compute points without a browser. Duplication like
that drifts silently unless something holds it in place.

Two things hold it. TestTheTwoImplementationsAgree reads DEFAULT_SCORING
straight out of the app template and asserts it matches scoring.DEFAULTS, so a
value changed in one place and not the other fails immediately. The numeric
fixtures below were read off the running app and are asserted here, so a change
to the *shape* of the formula fails too. If either of those fires, that is the
alarm working — do not paper over it.
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scoring   # noqa: E402

SC = scoring.DEFAULTS
APP = Path(__file__).resolve().parent.parent / "app" / "octagon-draft.html"

GAETHJE = "9e8f6c728eb01124"

# UFC 324 — Gaethje def. Pimblett (#5) by decision in a 5-round title bout,
# Fight of the Night. The app renders this as 19.1.
PIMBLETT_BOUT = {
    "done": True, "winner_id": GAETHJE, "method": "DEC", "round": 5,
    "title": True, "perf": True, "outcome": "win",
    "stats": {GAETHJE: {"sig": 27, "td": 0, "kd": 1, "sub": 0, "ctrl": 61}},
}

# UFC Freedom 250 — Gaethje def. Topuria (#1) by KO in round 4 of a title bout,
# Performance of the Night. The app renders this as 27.3.
TOPURIA_BOUT = {
    "done": True, "winner_id": GAETHJE, "method": "KO/TKO", "round": 4,
    "title": True, "perf": True, "outcome": "win",
    "stats": {GAETHJE: {"sig": 32, "td": 0, "kd": 0, "sub": 0, "ctrl": 0}},
}


def scoring_table_from_app() -> tuple[int, dict]:
    html = APP.read_text(encoding="utf-8")
    version = int(re.search(r"var\s+SCORING_VERSION\s*=\s*(\d+)", html).group(1))
    body = re.search(r"var\s+DEFAULT_SCORING\s*=\s*\{(.*?)\};", html, re.S).group(1)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\1":', body).strip().rstrip(",")
    return version, json.loads("{" + body + "}")


class TestTheTwoImplementationsAgree(unittest.TestCase):
    """The strongest lock available: the numbers themselves, compared."""

    def test_every_value_matches_the_app(self):
        _, table = scoring_table_from_app()
        for key, value in table.items():
            with self.subTest(key=key):
                self.assertIn(key, SC, f"{key} is in the app but not in scoring.py")
                self.assertEqual(SC[key], value,
                                 f"{key}: app says {value}, scoring.py says {SC[key]}")

    def test_scoring_py_invents_nothing_the_app_does_not_have(self):
        _, table = scoring_table_from_app()
        self.assertEqual(sorted(SC), sorted(table))

    def test_the_version_stamps_match(self):
        version, _ = scoring_table_from_app()
        self.assertEqual(version, scoring.SCORING_VERSION)

    def test_the_seed_state_ships_the_same_table(self):
        """The built page carries the table so the notifier can read it back."""
        html = APP.read_text(encoding="utf-8")
        seed = json.loads(re.search(
            r'<script id="league-state" type="application/json">(.*?)</script>',
            html, re.S).group(1).replace("<\\/", "</"))
        _, table = scoring_table_from_app()
        self.assertEqual(seed["scoring"], table)
        self.assertEqual(seed["scoringVersion"], scoring.SCORING_VERSION)


class TestParityWithTheApp(unittest.TestCase):
    def test_decision_win_over_a_top_five_opponent(self):
        got = scoring.bout_points(PIMBLETT_BOUT, GAETHJE, 5, SC)
        self.assertAlmostEqual(got["points"], 19.1, places=1)

    def test_knockout_win_over_the_number_one_contender(self):
        got = scoring.bout_points(TOPURIA_BOUT, GAETHJE, 1, SC)
        self.assertAlmostEqual(got["points"], 27.3, places=1)

    def test_the_breakdown_sums_to_the_total(self):
        for bout, rank in ((PIMBLETT_BOUT, 5), (TOPURIA_BOUT, 1)):
            got = scoring.bout_points(bout, GAETHJE, rank, SC)
            self.assertAlmostEqual(sum(v for _, v in got["lines"]),
                                   got["points"], places=2)


class TestTheScaleIsTheOneThatWasAskedFor(unittest.TestCase):
    """A season should land near 200 and rarely pass 250. These are the
    per-fight anchors that produce that, and they are worth failing on: a
    reverted or half-applied rescale shows up here rather than in March."""

    def test_a_quiet_win_is_single_figures(self):
        bout = {"done": True, "winner_id": "me", "method": "DEC", "round": 3,
                "outcome": "win", "title": False, "perf": False,
                "stats": {"me": {"sig": 60, "td": 2, "kd": 0, "sub": 0, "ctrl": 300}}}
        self.assertLess(scoring.bout_points(bout, "me", None, SC)["points"], 10)

    def test_the_ceiling_is_a_quarter_of_a_season_not_a_season(self):
        """Physically impossible in one bout — a first-round title knockout of
        the champion that somehow also has four hundred seconds of control and
        sixty significant strikes — so this is the hard ceiling, not a target.
        The best night in the real twelve months of data scores about 34."""
        bout = {"done": True, "winner_id": "me", "method": "KO/TKO", "round": 1,
                "outcome": "win", "title": True, "perf": True,
                "stats": {"me": {"sig": 60, "td": 3, "kd": 3, "sub": 2, "ctrl": 400}}}
        self.assertLess(scoring.bout_points(bout, "me", 0, SC)["points"], 60)

    def test_a_realistic_great_night_is_in_the_twenties(self):
        bout = {"done": True, "winner_id": "me", "method": "KO/TKO", "round": 2,
                "outcome": "win", "title": False, "perf": True,
                "stats": {"me": {"sig": 34, "td": 1, "kd": 2, "sub": 0, "ctrl": 180}}}
        got = scoring.bout_points(bout, "me", 8, SC)["points"]
        self.assertGreater(got, 20)
        self.assertLess(got, 35)


class TestEndingsThatAreNotFinishes(unittest.TestCase):
    """ufcstats reports more than three endings. Treating everything that is not
    a decision or a submission as a knockout paid knockout money — plus a finish
    bonus — for disqualifications and doctor stoppages alike."""

    BASE = {"done": True, "winner_id": "me", "round": 1, "title": False,
            "perf": False, "outcome": "win", "stats": {}}

    def points(self, **kw):
        return scoring.bout_points(dict(self.BASE, **kw), "me", None, SC)["points"]

    def test_a_knockout_is_a_finish(self):
        self.assertEqual(self.points(method="KO/TKO"),
                         SC["appear"] + SC["winKo"] + SC["fin1"])

    def test_a_submission_is_a_finish(self):
        self.assertEqual(self.points(method="SUB"),
                         SC["appear"] + SC["winSub"] + SC["fin1"])

    def test_a_disqualification_is_not(self):
        self.assertEqual(self.points(method="DQ"), SC["appear"] + SC["winDec"])

    def test_could_not_continue_is_not(self):
        self.assertEqual(self.points(method="Could Not Continue"),
                         SC["appear"] + SC["winDec"])

    def test_an_unrecognised_method_falls_back_to_a_decision(self):
        self.assertEqual(self.points(method="Overturned"), SC["appear"] + SC["winDec"])

    def test_losing_by_disqualification_costs_the_finish_penalty(self):
        self.assertEqual(self.points(winner_id="them", method="DQ"),
                         SC["appear"] + SC["lossFin"])

    def test_losing_a_decision_costs_the_lighter_one(self):
        self.assertEqual(self.points(winner_id="them", method="DEC"),
                         SC["appear"] + SC["lossDec"])


class TestDrawsAndNoContests(unittest.TestCase):
    STATS = {"me": {"sig": 50, "td": 0, "kd": 0, "sub": 0, "ctrl": 0}}

    def test_a_draw_pays_the_draw_value_on_top_of_the_appearance(self):
        bout = {"done": True, "winner_id": None, "method": "DEC", "round": 3,
                "outcome": "draw", "title": False, "perf": False, "stats": {}}
        self.assertEqual(scoring.bout_points(bout, "me", None, SC)["points"],
                         SC["appear"] + SC["draw"])

    def test_a_no_contest_pays_the_appearance_and_the_work(self):
        bout = {"done": True, "winner_id": None, "method": "NC", "round": 1,
                "outcome": "nc", "title": False, "perf": False, "stats": self.STATS}
        got = scoring.bout_points(bout, "me", None, SC)["points"]
        self.assertAlmostEqual(got, SC["appear"] + round(50 * SC["sig"], 1), places=1)

    def test_a_no_contest_pays_no_result(self):
        """Even though a winner id may linger on an overturned bout."""
        bout = {"done": True, "winner_id": "me", "method": "KO/TKO", "round": 1,
                "outcome": "nc", "title": False, "perf": True, "stats": {}}
        self.assertEqual(scoring.bout_points(bout, "me", None, SC)["points"], SC["appear"])


class TestOpponentQuality(unittest.TestCase):
    BASE = {"done": True, "winner_id": "me", "method": "DEC", "round": 3,
            "title": False, "perf": False, "outcome": "win", "stats": {}}

    def points(self, rank, winner="me"):
        return scoring.bout_points(dict(self.BASE, winner_id=winner), "me", rank, SC)["points"]

    def test_ladder_increases_with_opponent_rank(self):
        unranked, ranked, top10, top5, champ = (self.points(r) for r in (None, 12, 8, 3, 0))
        self.assertEqual(unranked, 6.0)                       # 2 + 4, no multiplier
        self.assertLess(unranked, ranked)
        self.assertLess(ranked, top10)
        self.assertLess(top10, top5)
        self.assertLess(top5, champ)
        self.assertAlmostEqual(champ, 12.0, places=1)         # doubled

    def test_a_losing_night_still_scales_up_while_it_stays_positive(self):
        """Appearance points mean most losses are still a net-positive night, so
        the multiplier applies normally: losing to the champion pays more than
        losing to an unranked fighter. That is the intended shape — taking the
        hard fight is worth something even when you lose it."""
        to_champ = self.points(0, winner="them")
        to_nobody = self.points(None, winner="them")
        self.assertAlmostEqual(to_nobody, 1.0, places=1)       # 2 appearance - 1
        self.assertAlmostEqual(to_champ, 2.0, places=1)        # doubled
        self.assertGreater(to_champ, to_nobody)

    def test_a_genuinely_bad_night_is_divided_instead(self):
        """When the night nets negative the multiplier inverts, so a mauling by
        the champion costs half what the same mauling by a nobody costs."""
        harsh = dict(SC, appear=0, statsOn=False)
        bout = dict(self.BASE, winner_id="them", method="KO/TKO")
        to_champ = scoring.bout_points(bout, "me", 0, harsh)["points"]
        to_nobody = scoring.bout_points(bout, "me", None, harsh)["points"]
        self.assertAlmostEqual(to_nobody, -2.0, places=1)
        self.assertAlmostEqual(to_champ, -1.0, places=1)
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
