"""Folding two copies of the database together.

Two machines write this file — a GitHub runner on a schedule, the PC whenever
somebody runs catch-up.ps1 — and they diverge by design, because the PC can
scrape ufcstats on days the runner is refused. Until 9 Sep the publish step
tried to reconcile that with `git pull --rebase`, which to git is a binary file
two people rewrote: conflict, halted rebase, failed run, email.

So these tests are about one property above all others: **nothing either
machine learned is thrown away**. Every case below is a real way the two copies
differ in practice.
"""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.a = store.connect(Path(self.dir.name) / "a.db")   # "here"
        self.b_path = Path(self.dir.name) / "b.db"
        self.b = store.connect(self.b_path)                     # "the other one"

    def merge(self):
        self.b.commit()
        self.b.close()
        return store.merge_from(self.a, self.b_path)

    def one(self, sql, *args):
        return self.a.execute(sql, args).fetchone()


def event(con, eid, date="2026-09-05", status="scheduled", scraped_at=100.0, name=None):
    con.execute("INSERT OR REPLACE INTO events (event_id,name,date,status,scraped_at)"
                " VALUES (?,?,?,?,?)", (eid, name or eid, date, status, scraped_at))


def bout(con, bid, eid="e1", status="announced", updated_at=100.0,
         winner=None, method="", a="fa", b="fb"):
    con.execute("""INSERT OR REPLACE INTO bouts
        (bout_id,event_id,fighter_a,fighter_b,status,winner_id,method,updated_at)
        VALUES (?,?,?,?,?,?,?,?)""", (bid, eid, a, b, status, winner, method, updated_at))


def fighter(con, fid, name="Fighter", rank=None, updated_at=100.0,
            on_roster=None, roster_at=None, wins=0):
    con.execute("""INSERT OR REPLACE INTO fighters
        (fighter_id,name,rank,wins,updated_at,on_roster,roster_at)
        VALUES (?,?,?,?,?,?,?)""",
                (fid, name, rank, wins, updated_at, on_roster, roster_at))


def stats_row(con, bid, fid, sig_att=0, tot_att=0, td_att=0, ctrl=0, sig_landed=0):
    con.execute("""INSERT OR REPLACE INTO bout_stats
        (bout_id,fighter_id,sig_str_landed,sig_str_attempted,total_str_attempted,
         td_attempted,ctrl_sec) VALUES (?,?,?,?,?,?,?)""",
                (bid, fid, sig_landed, sig_att, tot_att, td_att, ctrl))


class TestAScrapedCardSurvives(MergeCase):
    """The PC scraped Saturday's card. The runner was refused and knows nothing
    about it. That result is the whole point of the league."""

    def test_a_completed_card_beats_an_announced_one_whatever_the_clock_says(self):
        event(self.a, "e1", status="scheduled", scraped_at=999.0)      # newer, but ignorant
        bout(self.a, "b1", status="announced", updated_at=999.0)
        event(self.b, "e1", status="completed", scraped_at=1.0)        # older, but knows
        bout(self.b, "b1", status="completed", updated_at=1.0,
             winner="fa", method="KO/TKO")
        self.merge()
        self.assertEqual(self.one("SELECT status FROM events WHERE event_id='e1'")[0],
                         "completed")
        row = self.one("SELECT status,winner_id,method FROM bouts WHERE bout_id='b1'")
        self.assertEqual(tuple(row), ("completed", "fa", "KO/TKO"))

    def test_a_card_only_the_other_copy_has_comes_across_whole(self):
        event(self.b, "e2", name="Noche UFC", status="completed")
        bout(self.b, "b2", eid="e2", status="completed", winner="fa")
        moved = self.merge()
        self.assertEqual(moved["events"], 1)
        self.assertEqual(self.one("SELECT name FROM events WHERE event_id='e2'")[0],
                         "Noche UFC")

    def test_a_card_only_this_copy_has_is_not_removed(self):
        event(self.a, "mine", status="completed")
        self.merge()
        self.assertIsNotNone(self.one("SELECT 1 FROM events WHERE event_id='mine'"))


class TestARosterPassIsNotUndone(MergeCase):
    """The failure this whole exercise exists to prevent: a fighter row written
    by a fighter-page refresh quietly reverting a roster pass, so every released
    fighter becomes signable again and nothing anywhere says why."""

    def test_a_newer_fighter_page_does_not_revert_a_release(self):
        fighter(self.a, "f1", name="Old Name", rank=5,
                updated_at=10.0, on_roster=0, roster_at=500.0)   # we know: released
        fighter(self.b, "f1", name="New Name", rank=3,
                updated_at=900.0, on_roster=None, roster_at=None)  # newer, but never asked
        self.merge()
        r = self.one("SELECT name,rank,on_roster,roster_at FROM fighters WHERE fighter_id='f1'")
        self.assertEqual(r["name"], "New Name")   # the fresher page wins the page fields
        self.assertEqual(r["rank"], 3)
        self.assertEqual(r["on_roster"], 0)       # the roster pass keeps the roster fields
        self.assertEqual(r["roster_at"], 500.0)

    def test_a_newer_roster_pass_does_win(self):
        fighter(self.a, "f1", on_roster=0, roster_at=100.0, updated_at=900.0)
        fighter(self.b, "f1", on_roster=1, roster_at=800.0, updated_at=1.0)
        self.merge()
        self.assertEqual(self.one("SELECT on_roster FROM fighters WHERE fighter_id='f1'")[0], 1)

    def test_an_older_fighter_page_changes_nothing(self):
        fighter(self.a, "f1", name="Current", rank=1, updated_at=900.0)
        fighter(self.b, "f1", name="Stale", rank=9, updated_at=10.0)
        self.merge()
        r = self.one("SELECT name,rank FROM fighters WHERE fighter_id='f1'")
        self.assertEqual(tuple(r), ("Current", 1))


class TestStatisticsPreferTheWholeFight(MergeCase):
    """Rows scraped before the totals-table fix hold round one's numbers rather
    than the fight's, so they are strictly smaller for the same bout."""

    def test_the_fuller_row_wins_regardless_of_which_side_it_is_on(self):
        stats_row(self.a, "b1", "fa", sig_att=20, tot_att=25, td_att=1, ctrl=60)
        stats_row(self.b, "b1", "fa", sig_att=140, tot_att=190, td_att=6, ctrl=420)
        self.merge()
        self.assertEqual(self.one("SELECT sig_str_attempted FROM bout_stats"
                                  " WHERE bout_id='b1'")[0], 140)

    def test_a_thinner_row_does_not_overwrite_a_fuller_one(self):
        stats_row(self.a, "b1", "fa", sig_att=140, tot_att=190, td_att=6, ctrl=420)
        stats_row(self.b, "b1", "fa", sig_att=20, tot_att=25, td_att=1, ctrl=60)
        self.merge()
        self.assertEqual(self.one("SELECT sig_str_attempted FROM bout_stats"
                                  " WHERE bout_id='b1'")[0], 140)


class TestTheSmallTables(MergeCase):
    def test_flags_are_a_union(self):
        self.a.execute("INSERT INTO flags VALUES ('b1','fa','missed_weight')")
        self.b.execute("INSERT INTO flags VALUES ('b1','fb','withdrew')")
        self.b.execute("INSERT INTO flags VALUES ('b1','fa','missed_weight')")
        moved = self.merge()
        self.assertEqual(moved["flags"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM flags")[0], 2)

    def test_an_alias_is_carried_across(self):
        """A fighter drafted before they had ever fought. Lose this and the
        manager holding them loses a roster spot."""
        self.b.execute("INSERT INTO aliases VALUES ('w-temp','realid',5.0)")
        self.merge()
        self.assertEqual(self.one("SELECT to_id FROM aliases WHERE from_id='w-temp'")[0],
                         "realid")

    def test_both_machines_run_logs_survive(self):
        store.log_run(self.a, "nightly", time.time() - 60, True, "runner")
        store.log_run(self.b, "nightly", time.time() - 30, True, "the PC")
        moved = self.merge()
        self.assertEqual(moved["runs"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM runs")[0], 2)

    def test_the_same_run_is_not_logged_twice(self):
        started = time.time() - 60
        for con in (self.a, self.b):
            store.log_run(con, "nightly", started, True, "same run, both copies")
        self.merge()
        self.assertEqual(self.one("SELECT COUNT(*) FROM runs")[0], 1)


class TestItIsSafeToRunTwice(MergeCase):
    def test_merging_the_same_file_again_moves_nothing(self):
        event(self.b, "e1", status="completed", scraped_at=5.0)
        bout(self.b, "b1", status="completed", updated_at=5.0)
        fighter(self.b, "f1", updated_at=5.0, on_roster=1, roster_at=5.0)
        stats_row(self.b, "b1", "fa", sig_att=100)
        self.b.commit()
        self.b.close()
        first = store.merge_from(self.a, self.b_path)
        second = store.merge_from(self.a, self.b_path)
        self.assertTrue(any(first.values()))
        self.assertEqual(sum(second.values()), 0, f"second merge moved {second}")


class TestNothingIsLostEitherWay(MergeCase):
    """The realistic case: each side has something the other does not."""

    def test_the_runners_rankings_and_the_pcs_card_both_end_up_in_one_file(self):
        # this copy: a rankings/roster pass, no new card
        for i in range(5):
            fighter(self.a, f"f{i}", rank=i, updated_at=900.0,
                    on_roster=1, roster_at=900.0)
        # the other copy: Saturday's card, but a stale view of the fighters
        event(self.b, "card", status="completed", scraped_at=800.0)
        for i in range(5):
            fighter(self.b, f"f{i}", rank=None, updated_at=1.0)
            bout(self.b, f"bb{i}", eid="card", status="completed",
                 updated_at=800.0, winner=f"f{i}")
        self.merge()
        self.assertEqual(self.one("SELECT COUNT(*) FROM bouts WHERE status='completed'")[0], 5)
        self.assertEqual(self.one("SELECT COUNT(*) FROM fighters WHERE rank IS NOT NULL")[0], 5)
        self.assertEqual(self.one("SELECT COUNT(*) FROM fighters WHERE on_roster=1")[0], 5)


class TestAnOlderSchemaStillMerges(MergeCase):
    """A copy made before on_roster/roster_at existed must not blow up."""

    def test_a_database_missing_later_columns_is_upgraded_and_merged(self):
        old = Path(self.dir.name) / "old.db"
        con = sqlite3.connect(old)
        con.executescript("""
            CREATE TABLE fighters (fighter_id TEXT PRIMARY KEY, name TEXT NOT NULL,
              nickname TEXT DEFAULT '', wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
              draws INTEGER DEFAULT 0, division TEXT DEFAULT '', rank INTEGER,
              updated_at REAL);
            INSERT INTO fighters (fighter_id,name,updated_at) VALUES ('old1','Older',900.0);
        """)
        con.commit()
        con.close()
        store.merge_from(self.a, old)
        self.assertEqual(self.one("SELECT name FROM fighters WHERE fighter_id='old1'")[0],
                         "Older")


if __name__ == "__main__":
    unittest.main(verbosity=2)
