"""Parser tests against captured fixtures.

These prove the parsing *logic* is right: ids, dates, winner ordering, method
normalisation, stat extraction, name matching, and the exported shape. What they
cannot prove is that ufcstats' markup still looks like the fixtures — only
`python pipeline.py verify` does that, against the live site. Run both.
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import normalize        # noqa: E402
import store            # noqa: E402
from sources import ufcstats, wikipedia   # noqa: E402

FIX = Path(__file__).parent / "fixtures"
read = lambda n: (FIX / n).read_text()


class TestEventsIndex(unittest.TestCase):
    def test_rows(self):
        rows = ufcstats.parse_events_index(read("events_index.html"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_id"], "aaa1111111111111")
        self.assertEqual(rows[0]["name"], "UFC 319: Du Plessis vs. Chimaev")
        self.assertEqual(rows[0]["date"], "2026-08-16")
        self.assertEqual(rows[0]["location"], "Chicago, Illinois, USA")

    def test_empty_table_is_an_error(self):
        with self.assertRaises(ufcstats.ParseError):
            ufcstats.parse_events_index("<html><body><p>nope</p></body></html>")


class TestEventPage(unittest.TestCase):
    def setUp(self):
        self.ev = ufcstats.parse_event(
            read("event.html"),
            url="http://ufcstats.com/event-details/aaa1111111111111")

    def test_metadata(self):
        self.assertEqual(self.ev["name"], "UFC 319: Du Plessis vs. Chimaev")
        self.assertEqual(self.ev["date"], "2026-08-16")
        self.assertEqual(self.ev["event_id"], "aaa1111111111111")

    def test_winner_is_listed_first(self):
        main = self.ev["bouts"][0]
        self.assertEqual(main["winner_id"], "aaa000000000000a")
        self.assertEqual(main["fighters"][0]["name"], "Khamzat Chimaev")
        self.assertEqual(main["outcome"], "win")

    def test_title_and_weight_class(self):
        main = self.ev["bouts"][0]
        self.assertTrue(main["title_bout"])
        self.assertEqual(main["weight_class"], "Middleweight")
        self.assertFalse(self.ev["bouts"][1]["title_bout"])

    def test_method_normalised(self):
        self.assertEqual(self.ev["bouts"][0]["method"], "DEC")
        self.assertEqual(self.ev["bouts"][0]["round"], 5)
        self.assertEqual(self.ev["bouts"][1]["method"], "KO/TKO")
        self.assertEqual(self.ev["bouts"][1]["round"], 1)

    def test_short_row_is_an_error(self):
        broken = read("event.html").replace(
            '<td class="b-fight-details__table-col"><p class="b-fight-details__table-text">5:00</p></td>', "")
        with self.assertRaises(ufcstats.ParseError):
            ufcstats.parse_event(broken, url="x")


class TestFightPage(unittest.TestCase):
    def setUp(self):
        self.f = ufcstats.parse_fight(
            read("fight.html"),
            url="http://ufcstats.com/fight-details/f00d000000000002")

    def test_result(self):
        self.assertEqual(self.f["winner_id"], "ccc000000000000c")
        self.assertEqual(self.f["outcome"], "win")
        self.assertEqual(self.f["method"], "KO/TKO")
        self.assertEqual(self.f["round"], 1)
        self.assertEqual(self.f["time"], "4:16")
        self.assertEqual(self.f["referee"], "Dan Miragliotta")

    def test_bonuses(self):
        self.assertIn("performance", self.f["bonuses"])

    def test_totals_not_per_round(self):
        """The per-round table must never be mistaken for the totals table."""
        win = self.f["stats"]["ccc000000000000c"]
        lose = self.f["stats"]["ddd000000000000d"]
        self.assertEqual(win["kd"], 1)
        self.assertEqual(win["sig_str_landed"], 18)
        self.assertEqual(win["sig_str_attempted"], 30)
        self.assertEqual(win["td_landed"], 1)
        self.assertEqual(win["td_attempted"], 2)
        self.assertEqual(win["ctrl_sec"], 84)          # 1:24
        self.assertEqual(lose["sig_str_landed"], 9)
        self.assertEqual(lose["sub_att"], 1)
        self.assertEqual(lose["ctrl_sec"], 11)

    def test_missing_fighters_is_an_error(self):
        with self.assertRaises(ufcstats.ParseError):
            ufcstats.parse_fight("<html><body></body></html>", url="x")


class TestWikipedia(unittest.TestCase):
    def setUp(self):
        self.card = wikipedia.parse_event_card(read("wiki_event.html"))

    def test_completed_and_announced_bouts(self):
        completed = [b for b in self.card["bouts"] if b["status"] == "completed"]
        announced = [b for b in self.card["bouts"] if b["status"] == "announced"]
        self.assertEqual(completed[0]["winner"], "Khamzat Chimaev")
        self.assertEqual(completed[0]["weight_class"], "Middleweight")
        self.assertEqual(len(announced), 2)
        self.assertEqual(announced[0]["fighters"], ["Arman Tsarukyan", "Dan Hooker"])

    def test_bonuses(self):
        kinds = {b["type"] for b in self.card["bonuses"]}
        names = {b["fighter"] for b in self.card["bonuses"]}
        self.assertEqual(kinds, {"fight_of_night", "performance"})
        self.assertIn("Lerone Murphy", names)

    def test_missed_weight(self):
        self.assertTrue(any(n["fighter"] == "Roman Dolidze"
                            for n in self.card["weigh_in_notes"]))

class TestRankings(unittest.TestCase):
    """Three shapes have already broken this parser once each, so all three are
    pinned: rank in a row-header <th>, an empty ISO column sitting between rank
    and fighter, and every division appearing twice on the page."""

    def setUp(self):
        self.r = wikipedia.parse_rankings(read("wiki_rankings.html"))

    def test_finds_fighter_past_the_iso_column(self):
        self.assertEqual(self.r["Heavyweight"][0], "Tom Aspinall")
        self.assertEqual(self.r["Light Heavyweight"][0], "Carlos Ulberg")

    def test_interim_champion_sits_between_champion_and_number_one(self):
        self.assertEqual(self.r["Heavyweight"][:3],
                         ["Tom Aspinall", "Ciryl Gane", "Alexander Volkov"])

    def test_first_block_wins_over_the_duplicate(self):
        self.assertNotIn("SHOULD NOT WIN", self.r["Heavyweight"])

    def test_light_heavyweight_is_not_filed_as_heavyweight(self):
        """'Light Heavyweight' contains 'Heavyweight'; longest match must win."""
        self.assertEqual(self.r["Light Heavyweight"][1], "Alex Pereira")
        self.assertNotIn("Alex Pereira", self.r["Heavyweight"])

    def test_womens_division_is_not_filed_as_mens(self):
        self.assertIn("Women's Bantamweight", self.r)
        self.assertEqual(self.r["Women's Bantamweight"][0], "Kayla Harrison")
        self.assertNotIn("Bantamweight", self.r)

    def test_legend_table_ignored(self):
        self.assertNotIn("Move up", str(self.r))


class TestDivisionMatching(unittest.TestCase):
    """The shortest-first bug this catches put half the roster in the wrong
    division: 'Light Heavyweight' matched 'Heavyweight', and every women's
    division matched its men's counterpart."""

    def test_ufcstats_weight_classes(self):
        cases = {
            "Light Heavyweight Bout": "Light Heavyweight",
            "Heavyweight Bout": "Heavyweight",
            "Women's Bantamweight Title Bout": "Women's Bantamweight",
            "Bantamweight Bout": "Bantamweight",
            "Women's Strawweight Bout": "Women's Strawweight",
            "Women's Flyweight Bout": "Women's Flyweight",
            "Flyweight Bout": "Flyweight",
            "Catch Weight Bout": "Catch Weight",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ufcstats._weight_class(raw), expected)


class TestNameMatching(unittest.TestCase):
    def setUp(self):
        self.con = store.connect(":memory:")
        for fid, name in [("a", "Alexandre Pantoja"), ("b", "José Aldo"),
                          ("c", "Khalil Rountree Jr."), ("d", "Song Yadong")]:
            store.upsert_fighter(self.con, {"fighter_id": fid, "name": name})
        self.con.commit()
        self.idx = normalize.NameIndex(self.con)

    def test_exact_and_accents(self):
        self.assertEqual(self.idx.match("Alexandre Pantoja"), "a")
        self.assertEqual(self.idx.match("Jose Aldo"), "b")
        self.assertEqual(self.idx.match("JOSÉ  ALDO"), "b")

    def test_suffixes(self):
        self.assertEqual(self.idx.match("Khalil Rountree"), "c")

    def test_unknown_returns_none(self):
        self.assertIsNone(self.idx.match("Someone Entirely Else"))
        self.assertIsNone(self.idx.match(""))


class TestStoreAndExport(unittest.TestCase):
    """End-to-end over the real SQL: parse -> store -> export."""

    def setUp(self):
        self.con = store.connect(":memory:")
        ev = ufcstats.parse_event(read("event.html"),
                                  url="http://ufcstats.com/event-details/aaa1111111111111")
        ev["status"] = "completed"
        store.upsert_event(self.con, ev)
        detail = ufcstats.parse_fight(read("fight.html"),
                                      url="http://ufcstats.com/fight-details/f00d000000000002")
        for pos, b in enumerate(ev["bouts"]):
            for f in b["fighters"]:
                store.upsert_fighter(self.con, {"fighter_id": f["fighter_id"],
                                                "name": f["name"], "wins": 10, "losses": 2})
            merged = dict(b, status="completed")
            if b["bout_id"] == "f00d000000000002":
                merged["stats"] = detail["stats"]
                merged["bonuses"] = detail["bonuses"]
            store.upsert_bout(self.con, ev["event_id"], merged, pos)
        self.con.commit()

    def test_idempotent(self):
        before = store.stats(self.con)
        ev = ufcstats.parse_event(read("event.html"),
                                  url="http://ufcstats.com/event-details/aaa1111111111111")
        store.upsert_event(self.con, dict(ev, status="completed"))
        for pos, b in enumerate(ev["bouts"]):
            store.upsert_bout(self.con, ev["event_id"], dict(b, status="completed"), pos)
        self.con.commit()
        self.assertEqual(before, store.stats(self.con))

    def test_divisions_inferred(self):
        normalize.infer_divisions(self.con)
        row = self.con.execute(
            "SELECT division FROM fighters WHERE fighter_id='ccc000000000000c'").fetchone()
        self.assertEqual(row["division"], "Featherweight")

    def test_export_shape(self):
        normalize.infer_divisions(self.con)
        out = Path("/tmp/_export_test.json")
        payload = normalize.export(self.con, out, since="2020-01-01")
        self.assertEqual(len(payload["events"]), 1)
        bouts = payload["events"][0]["bouts"]
        self.assertEqual(len(bouts), 2)

        main = bouts[0]
        self.assertTrue(main["title"])
        self.assertEqual(main["rounds"], 5)          # title bout
        self.assertEqual(main["win"], "aaa000000000000a")
        self.assertTrue(main["done"])

        second = bouts[1]
        self.assertEqual(second["rounds"], 3)        # not main, not title
        self.assertTrue(second["perf"])
        self.assertEqual(second["st"]["ccc000000000000c"]["sig"], 18)
        self.assertEqual(second["st"]["ccc000000000000c"]["ctrl"], 84)

        self.assertEqual({f["div"] for f in payload["fighters"]}, {"MW", "FW"})
        self.assertEqual(json.loads(out.read_text())["events"], payload["events"])


class TestRoster(unittest.TestCase):
    """The roster list is what makes a signed-but-undebuted fighter draftable
    and stops a released one from sitting in the pool. It is also an article
    full of tables that are not divisions, which is where this gets interesting.
    """

    def setUp(self):
        self.roster = wikipedia.parse_roster(
            (FIX / "roster.html").read_text(encoding="utf-8"))

    def test_reads_divisions(self):
        self.assertEqual(set(self.roster), {"Heavyweight", "Women's Strawweight"})

    def test_finds_the_name_column_past_the_flag(self):
        # Column 0 is a flag that renders as empty text; counting from the left
        # gets you nothing at all.
        self.assertEqual(self.roster["Heavyweight"][:2], ["Derrick Lewis", "Curtis Blaydes"])

    def test_keeps_fighters_with_no_article(self):
        # A new signing often has no Wikipedia page — plain text, not a link.
        self.assertIn("Alvin Hines", self.roster["Heavyweight"])

    def test_strips_the_champion_marker(self):
        self.assertIn("Champ McChampface", self.roster["Heavyweight"])

    def test_ignores_the_tables_that_are_not_divisions(self):
        everyone = [n for names in self.roster.values() for n in names]
        for decoy in ("Ghost Signing", "Suspended Person", "Not A Division"):
            self.assertNotIn(decoy, everyone)


class TestApplyRoster(unittest.TestCase):
    def setUp(self):
        self.con = store.connect(":memory:")
        for fid, name in (("aaa000000000000a", "Derrick Lewis"),
                          ("bbb000000000000b", "Curtis Blaydes"),
                          ("ccc000000000000c", "Released Guy")):
            self.con.execute(
                "INSERT INTO fighters (fighter_id, name, updated_at) VALUES (?,?,0)",
                (fid, name))
        self.con.commit()

    def test_marks_contracted_adds_signings_and_drops_the_released(self):
        result = normalize.apply_roster(self.con, {
            "Heavyweight": ["Derrick Lewis", "Curtis Blaydes", "Alvin Hines"]})
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["added"], 1)

        rows = dict(self.con.execute("SELECT name, on_roster FROM fighters"))
        self.assertEqual(rows["Derrick Lewis"], 1)
        self.assertEqual(rows["Alvin Hines"], 1)          # signed, never fought
        self.assertEqual(rows["Released Guy"], 0)         # kept: he has history

    def test_placeholder_disappears_once_the_real_record_arrives(self):
        normalize.apply_roster(self.con, {"Heavyweight": ["Alvin Hines"]})
        placeholder = self.con.execute(
            "SELECT fighter_id FROM fighters WHERE name='Alvin Hines'").fetchone()[0]
        self.assertFalse(re.match(r"^[0-9a-f]{16}$", placeholder))

        # He debuts, so ufcstats now has him under a real id.
        self.con.execute("INSERT INTO fighters (fighter_id, name, updated_at) "
                         "VALUES ('ddd000000000000d','Alvin Hines',0)")
        normalize.apply_roster(self.con, {"Heavyweight": ["Alvin Hines"]})
        ids = [r[0] for r in self.con.execute(
            "SELECT fighter_id FROM fighters WHERE name='Alvin Hines'")]
        self.assertEqual(ids, ["ddd000000000000d"])

    def test_a_released_fighter_stays_in_the_export_if_he_fought(self):
        # Someone drafted him. His name, his record and his past scores still
        # have to render; he just cannot earn any more.
        self.con.execute("INSERT INTO events (event_id, name, date, status) "
                         "VALUES ('e1','UFC 1','2026-01-01','completed')")
        self.con.execute(
            "INSERT INTO bouts (bout_id, event_id, fighter_a, fighter_b, "
            "weight_class, status, outcome, winner_id, method, round, card_position) "
            "VALUES ('b1','e1','aaa000000000000a','ccc000000000000c',"
            "'Heavyweight','completed','win','aaa000000000000a','KO/TKO',1,0)")
        self.con.commit()

        normalize.apply_roster(self.con, {"Heavyweight": ["Derrick Lewis"]})
        payload = normalize.export(self.con, Path("/tmp/_roster_export.json"))
        by_name = {f["name"]: f for f in payload["fighters"]}
        self.assertEqual(by_name["Derrick Lewis"]["act"], 1)
        self.assertEqual(by_name["Released Guy"]["act"], 0)

    def test_a_debut_leaves_a_forwarding_address(self):
        """Someone drafted him before he had ever fought. When the real record
        arrives his id changes, and without a forwarding address the manager
        holding the old one silently loses a fighter."""
        normalize.apply_roster(self.con, {"Heavyweight": ["Alvin Hines"]})
        placeholder = self.con.execute(
            "SELECT fighter_id FROM fighters WHERE name='Alvin Hines'").fetchone()[0]

        self.con.execute("INSERT INTO fighters (fighter_id, name, updated_at) "
                         "VALUES ('ddd000000000000d','Alvin Hines',0)")
        normalize.apply_roster(self.con, {"Heavyweight": ["Alvin Hines"]})

        alias = dict(self.con.execute("SELECT from_id, to_id FROM aliases"))
        self.assertEqual(alias.get(placeholder), "ddd000000000000d")

        payload = normalize.export(self.con, Path("/tmp/_alias_export.json"))
        self.assertEqual(payload["alias"][placeholder], "ddd000000000000d")

    def test_a_broken_parse_leaves_last_weeks_roster_alone(self):
        """The dangerous failure is not an empty roster — it is a roster of
        strangers, which would bury the pool under placeholder duplicates."""
        for i in range(60):
            self.con.execute("INSERT INTO fighters (fighter_id, name, updated_at) "
                             "VALUES (?,?,0)", (f"{i:016x}", f"Real Fighter {i}"))
        self.con.commit()
        good = [f"Real Fighter {i}" for i in range(60)]
        normalize.apply_roster(self.con, {"Heavyweight": good})

        rubbish = good[:30] + [f"Nobody {i}" for i in range(40)]
        result = normalize.apply_roster(self.con, {"Heavyweight": rubbish})

        self.assertTrue(result["rejected"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM fighters WHERE on_roster=1").fetchone()[0], 60)

    def test_a_signing_with_no_fights_is_still_draftable(self):
        normalize.apply_roster(self.con, {"Heavyweight": ["Alvin Hines"]})
        payload = normalize.export(self.con, Path("/tmp/_roster_export2.json"))
        hines = [f for f in payload["fighters"] if f["name"] == "Alvin Hines"]
        self.assertEqual(len(hines), 1)
        self.assertEqual(hines[0]["act"], 1)
        self.assertEqual(hines[0]["div"], "HW")


if __name__ == "__main__":
    unittest.main(verbosity=2)
