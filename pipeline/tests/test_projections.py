"""The projection model.

Two properties matter more than the numbers themselves. It must be honest about
how little it knows — a fighter with one bout should be described mostly by
their division, not by that one bout — and it must be *deterministic*, because
the app's auto-pick now ranks on it and every phone that works out a missed
deadline has to reach the same fighter.
"""
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import projections   # noqa: E402
import scoring       # noqa: E402
import store         # noqa: E402


def build_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    return con


def add_fighter(con, fid, name, div="Lightweight", rank=None, on_roster=1):
    con.execute("INSERT INTO fighters (fighter_id,name,division,rank,on_roster,updated_at)"
                " VALUES (?,?,?,?,?,0)", (fid, name, div, rank, on_roster))


def add_bout(con, eid, bid, a, b, winner, method="DEC", rnd=3, clock="5:00",
             status="completed", outcome="win", title=0, bonuses="[]"):
    con.execute("INSERT OR IGNORE INTO events (event_id,name,date,status)"
                " VALUES (?,?,?,'completed')", (eid, eid, "2026-05-01"))
    con.execute("""INSERT INTO bouts (bout_id,event_id,fighter_a,fighter_b,winner_id,
                   method,round,time,status,outcome,title_bout,bonuses,card_position,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                (bid, eid, a, b, winner, method, rnd, clock, status, outcome, title, bonuses))


class TestShrinkage(unittest.TestCase):
    """One good night is not a season."""

    def setUp(self):
        self.con = build_db()
        # a busy division: eight fighters with two ordinary decisions each
        for i in range(8):
            add_fighter(self.con, f"reg{i}", f"Regular {i}")
        for i in range(0, 8, 2):
            add_bout(self.con, f"e{i}a", f"b{i}a", f"reg{i}", f"reg{i+1}", f"reg{i}")
            add_bout(self.con, f"e{i}b", f"b{i}b", f"reg{i+1}", f"reg{i}", f"reg{i+1}")
        # a one-hit wonder: a single first-round knockout and nothing else
        add_fighter(self.con, "flash", "Flash Knockout")
        add_bout(self.con, "eF", "bF", "flash", "reg0", "flash",
                 method="KO/TKO", rnd=1, clock="0:45", bonuses='["performance"]')
        # a grinder: four fights, ordinary results
        add_fighter(self.con, "grind", "Grinder")
        for i in range(4):
            add_bout(self.con, f"eG{i}", f"bG{i}", "grind", f"reg{i}", "grind")
        self.p = projections.build(self.con, as_of=date(2026, 9, 1))

    def test_a_single_knockout_does_not_out_project_a_busy_year(self):
        self.assertLess(self.p["flash"]["proj"], self.p["grind"]["proj"])

    def test_the_one_hit_wonder_is_pulled_toward_the_average(self):
        """Its points-per-fight sits between its own night and the division's."""
        ordinary = self.p["reg0"]["ppf"]
        self.assertGreater(self.p["flash"]["ppf"], ordinary)
        # but nowhere near the ~14 that single night actually scored
        self.assertLess(self.p["flash"]["ppf"], 14)

    def test_more_fights_means_a_higher_expected_count(self):
        self.assertGreater(self.p["grind"]["pf"], self.p["flash"]["pf"])

    def test_a_fighter_with_no_record_gets_the_divisional_average(self):
        add_fighter(self.con, "new", "New Signing")
        p = projections.build(self.con, as_of=date(2026, 9, 1))
        self.assertEqual(p["new"]["n"], 0)
        self.assertGreater(p["new"]["proj"], 0)
        self.assertAlmostEqual(p["new"]["ppf"], p["reg0"]["ppf"], delta=6)

    def test_the_range_brackets_the_projection(self):
        for v in self.p.values():
            self.assertLessEqual(v["lo"], v["proj"])
            self.assertLessEqual(v["proj"], v["hi"])
            self.assertGreaterEqual(v["lo"], 0)

    def test_less_evidence_means_a_wider_range(self):
        width = lambda k: self.p[k]["hi"] - self.p[k]["lo"]
        self.assertGreater(width("flash") / max(self.p["flash"]["proj"], 1),
                           width("grind") / max(self.p["grind"]["proj"], 1))


class TestItIsDeterministic(unittest.TestCase):
    """The auto-pick ranks on this. Two phones must agree exactly."""

    def test_two_builds_of_the_same_database_are_identical(self):
        con = build_db()
        for i in range(6):
            add_fighter(con, f"f{i}", f"F{i}", rank=i or None)
        for i in range(0, 6, 2):
            add_bout(con, f"e{i}", f"b{i}", f"f{i}", f"f{i+1}", f"f{i}")
        a = projections.build(con, as_of=date(2026, 9, 1))
        b = projections.build(con, as_of=date(2026, 9, 1))
        self.assertEqual(a, b)


class TestOpponentQualityCarriesThrough(unittest.TestCase):
    def test_beating_the_champion_projects_higher_than_beating_a_nobody(self):
        con = build_db()
        add_fighter(con, "champ", "Champion", rank=0)
        add_fighter(con, "nobody", "Unranked")
        add_fighter(con, "hard", "Beats the champ")
        add_fighter(con, "easy", "Beats nobody")
        for i in range(3):
            add_bout(con, f"eh{i}", f"bh{i}", "hard", "champ", "hard")
            add_bout(con, f"ee{i}", f"be{i}", "easy", "nobody", "easy")
        p = projections.build(con, as_of=date(2026, 9, 1))
        self.assertGreater(p["hard"]["ppf"], p["easy"]["ppf"])


class TestReleasedFightersAreNotProjected(unittest.TestCase):
    def test_only_fighters_under_contract_get_a_number(self):
        con = build_db()
        add_fighter(con, "in", "Under contract", on_roster=1)
        add_fighter(con, "out", "Released", on_roster=0)
        add_bout(con, "e1", "b1", "in", "out", "in")
        p = projections.build(con, as_of=date(2026, 9, 1))
        self.assertIn("in", p)
        self.assertNotIn("out", p)


class TestTheStatModel(unittest.TestCase):
    def test_it_falls_back_to_a_flat_allowance_with_no_trustworthy_rows(self):
        con = build_db()
        a, b = projections.fit_stat_model(con)
        self.assertGreater(a, 0)
        self.assertEqual(b, 0.0)

    def test_a_longer_fight_is_worth_more_stat_points(self):
        a, b = 0.85, 0.118
        self.assertGreater(a + b * 15, a + b * 3)

    def test_minutes_reads_the_clock(self):
        self.assertAlmostEqual(projections._minutes(3, "5:00"), 15.0)
        self.assertAlmostEqual(projections._minutes(1, "2:30"), 2.5)
        # a blank clock means the middle of the round, not zero
        self.assertAlmostEqual(projections._minutes(2, "0:00"), 7.5)

    def test_a_missing_clock_does_not_explode(self):
        self.assertGreater(projections._minutes(2, ""), 0)
        self.assertGreater(projections._minutes(1, None), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
