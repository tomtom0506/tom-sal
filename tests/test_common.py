"""Run: python -m unittest discover -s tests"""
import sqlite3, sys, types, unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common as c

# fetch.py imports israeli_prices at module level; a stub is enough for the pure DB logic tested here
sys.modules.setdefault("israeli_prices", types.SimpleNamespace(FileNotFound=Exception))
import fetch


class Normalize(unittest.TestCase):
    def test_date_none_is_not_the_string_none(self):  # bug: str(None) -> "None" was shown as "עד None"
        self.assertIsNone(c.norm_date(None))
        self.assertIsNone(c.norm_date("None"))
        self.assertEqual(c.norm_date(datetime(2026, 11, 20, 23, 59)), "2026-11-20 23:59:00")
        self.assertEqual(c.norm_date("2031-01-01 02:59:00"), "2031-01-01 02:59:00")

    def test_rate_percent_to_fraction(self):
        self.assertEqual(c.norm_rate(25.0), 0.25)
        self.assertEqual(c.norm_rate(100), 1.0)
        self.assertEqual(c.norm_rate(1), 0.01)
        self.assertEqual(c.norm_rate(0.18), 0.18)
        self.assertIsNone(c.norm_rate(None))
        self.assertIsNone(c.norm_rate(0))

    def test_club_formats_seen_in_real_files(self):
        for raw, want in [("0 - כלל הלקוחות", 0), ("0", 0), (None, 0), ("", 0), ("כלל הלקוחות", 0),
                          ("1 - המועדון החדש", 1), ("1=", 1), ("1=Yellow friends", 1), ("(3=פלטינום)", 3),
                          ("2 - מועדון אשראי של הקמעונאי", 2), (0, 0), (1, 1)]:
            self.assertEqual(c.norm_club(raw), want, raw)

    def test_min_qty_weighted(self):
        self.assertEqual(c.norm_qty(0.01), 1.0)
        self.assertEqual(c.norm_qty(None), 1.0)
        self.assertEqual(c.norm_qty(3), 3.0)


class Promos(unittest.TestCase):
    def test_coupon(self):
        self.assertTrue(c.is_coupon("קופון דבש 350 גרם - מתנה", 1, 1.0))
        self.assertTrue(c.is_coupon("קופון אישי 50 מעל 249", 1, None))
        self.assertTrue(c.is_coupon("100% הנחה", None, 1.0))       # bug: fraction 1.0 was not caught
        self.assertFalse(c.is_coupon("השני ב-90%", 2, 0.9))         # bug: a real promo was dropped as coupon
        self.assertFalse(c.is_coupon("2 ב 20", 2, 0.28))

    def test_active_window(self):  # bug: future promos were counted, start date ignored
        t = "2026-11-20"
        self.assertTrue(c.promo_active("2026-11-01", "2026-11-30", t))
        self.assertTrue(c.promo_active("2026-11-20 00:00:00", "2026-11-20 23:59:00", t))
        self.assertFalse(c.promo_active("2026-11-27", "2026-12-30", t))
        self.assertFalse(c.promo_active("2026-11-01", "2026-11-19", t))
        self.assertTrue(c.promo_active(None, None, t))


class Guard(unittest.TestCase):
    def test_publish_guard(self):  # bug: an empty run overwrote the only backup
        self.assertFalse(c.publish_ok(0, 0, 37)[0])
        self.assertFalse(c.publish_ok(0, 37, 37)[0])     # only fallback copies, nothing fresh
        self.assertFalse(c.publish_ok(10, 20, 37)[0])    # shrank below 70%
        self.assertTrue(c.publish_ok(30, 37, 37)[0])
        self.assertTrue(c.publish_ok(5, 5, 0)[0])        # first run ever

    def test_fresh_enough(self):
        t = date(2026, 11, 20)
        self.assertTrue(c.fresh_enough("2026-11-19", t))
        self.assertTrue(c.fresh_enough("2026-11-17", t))
        self.assertFalse(c.fresh_enough("2026-11-16", t))
        self.assertFalse(c.fresh_enough(None, t))


class Grouping(unittest.TestCase):
    def test_eggs(self):
        self.assertEqual(c.egg_group("ביצים L 12 יח'"), "ביצים L 12 יח׳")
        self.assertEqual(c.egg_group("ביצים 2X12 L"), "ביצים L 24 יח׳")   # bug: 24 eggs grouped as 12
        self.assertIsNone(c.egg_group("אטריות ביצים 500 גרם"))

    def test_size_tag(self):
        self.assertEqual(c.size_tag('גזר ארוז 1 ק"ג'), '1 ק"ג')
        self.assertEqual(c.size_tag("עגבניות שרי ארוז 500 גרם"), "500 גרם")
        self.assertEqual(c.size_tag("תאנים 500 גר"), "500 גרם")
        self.assertEqual(c.size_tag("חלב 3% 1 ליטר"), "1 ליטר")
        self.assertEqual(c.size_tag("עגבניה שרי במשקל"), "")
        self.assertEqual(c.size_tag("3 גבינות"), "")

    def test_packed_not_compared_to_per_kg(self):  # bug: 500g pack price compared to price per kg
        g = c.group_products([("shufersal:1", "עגבניות שרי ארוז 500 גרם", 0),
                              ("rami-levy:2", "עגבניה שרי במשקל", 1),
                              ("yellow:3", "עגבניה שרי", 1)])
        self.assertEqual(g["rami-levy:2"], g["yellow:3"])
        self.assertNotEqual(g["shufersal:1"], g["rami-levy:2"])

    def test_weighted_vs_unit_split(self):
        g = c.group_products([("a:1", "כרובית", 1), ("b:1", "כרובית", 1), ("c:1", "כרובית", 0)])
        self.assertEqual(g["a:1"], "כרובית")
        self.assertEqual(g["c:1"], "כרובית ליחידה")

    def test_bidi_marks_removed(self):
        self.assertEqual(c.norm_name("‫דג פילה סלמון קפוא"), "דג פילה סלמון קפוא")


class Fallback(unittest.TestCase):
    """bug: a store whose price download failed vanished; fallback only covered whole-chain failures."""

    def make_prev(self, dated):
        db = sqlite3.connect(":memory:")
        db.executescript(fetch.SCHEMA)
        db.execute("ATTACH DATABASE ':memory:' AS prev")
        db.executescript(fetch.SCHEMA.replace("CREATE TABLE ", "CREATE TABLE prev."))
        rows = [(1, "shufersal", "1", "A", dated), (2, "shufersal", "2", "B", dated), (3, "yellow", "7", "C", "2026-11-10")]
        for sid, ch, st, name, d in rows:
            db.execute("INSERT INTO prev.stores(sid,chain,store_id,name,address,city,items,promos,data_date) VALUES(?,?,?,?,?,?,1,1,?)",
                       (sid, ch, st, name, "", "", d))
            db.execute("INSERT INTO prev.products VALUES(?,?,?,?,?,?)", (f"{ch}:x{sid}", "x", "", 1, "", 0))
            db.execute("INSERT INTO prev.prices VALUES(?,?,?,?)", (f"{ch}:x{sid}", sid, 9.9, None))
            db.execute("INSERT INTO prev.promos(pid,sid,promotion_id,description,start,end,club,min_qty,discounted_price,discount_rate) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, sid, "p", "2 ב 15", "2026-11-01", "None", "1=", 2, 15, 25.0))
            db.execute("INSERT INTO prev.promo_items VALUES(?,?)", (f"{ch}:x{sid}", sid))
        return db

    def test_single_failed_store_is_kept_with_its_date(self):
        db = self.make_prev("2026-11-19")
        have = {("shufersal", "1")}
        out = fetch.copy_prev(db, date(2026, 11, 20), set(), {("shufersal", "2")}, have)
        self.assertEqual([r["store"] for r in out], ["B"])
        self.assertEqual(db.execute("SELECT stale, data_date FROM stores").fetchall(), [(1, "2026-11-19")])
        # promo fields are normalized on the way through
        self.assertEqual(db.execute("SELECT end, club, discount_rate FROM promos").fetchall(), [(None, 1, 0.25)])

    def test_old_fallback_is_dropped(self):  # bug: "yesterday" data was carried forever
        db = self.make_prev("2026-11-19")
        out = fetch.copy_prev(db, date(2026, 11, 20), {"yellow"}, set(), set())
        self.assertEqual(out, [])

    def test_whole_chain_failure(self):
        db = self.make_prev("2026-11-18")
        out = fetch.copy_prev(db, date(2026, 11, 20), {"shufersal"}, set(), set())
        self.assertEqual(sorted(r["store"] for r in out), ["A", "B"])


if __name__ == "__main__":
    unittest.main()
