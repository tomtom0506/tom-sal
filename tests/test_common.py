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


class Substitutes(unittest.TestCase):
    """Tomer's rule: same product in principle, similar size; ketchup for ketchup, never tomato paste."""
    P = {"heinz": "קטשופ היינץ 700 גרם", "osem": "אסם קטשופ בקבוק לחיץ 9*750גרם-חדש", "big": "קטשופ 1 ק\"ג",
         "paste": "רסק עגבניות 700 גרם", "spicy": "אסם קטשופ חריף 750 גרם", "bbq": "קטשופ ברביקיו 700 גרם",
         "low": "קטשופ היינץ ללא תוספת סוכר ומלח 700 גרם", "chips": "חטיף צ'יפס בטעם קטשופ 50 גרם",
         "pack": "מארז קטשופ אסם 750 גרם", "m3": "תנובה חלב 3% 1 ליטר", "m3b": "טרה חלב קרטון 3% 1ל", "m1": "חלב 1% 1 ליטר",
         "sh": "אלביב שמפו קולגן 500 מ\"ל", "co": "אלביב מרכך קולגן 500 מ\"ל", "d4": "האגיס אקסטרה קר מידה 4 32 יח'",
         "d5": "האגיס אקסטרה קר מידה 5 30 יח'", "hum1": "חומוס 500 גרם", "hum2": "חומוס צבר 500 גרם",
         "z1": "שוופס למון ליים 1.5 ליטר", "z2": "שוופס למון ליים ZERO 1.5 ליטר",
         "y1": "יוגורט עם קציפת תות 150 גרם", "y2": "יוגורט עם קציפת לימון 150 גרם",
         "gf1": "עוגיות ללא גלוטן שוקו 150 גרם", "gf2": "עוגיות ללא גלוטן לימון 150 גרם"}

    def subs(self):
        return c.substitutes(self.P)[0]

    def test_ketchup_brand_swap_similar_size(self):
        s = self.subs()
        self.assertIn("osem", [d for d, _ in s["heinz"]])
        self.assertNotIn("big", [d for d, _ in s["heinz"]])   # 1 kg is more than 20% from 700 g

    def test_never_a_different_product_or_variant(self):
        got = {d for v in self.subs().get("heinz", []) for d in v[:1]}
        for bad in ("paste", "spicy", "bbq", "low", "chips", "pack"):
            self.assertNotIn(bad, got, bad)

    def test_milk_percent_must_match(self):
        s = self.subs()
        self.assertEqual([d for d, _ in s["m3"]], ["m3b"])

    def test_type_words_and_identity_numbers(self):
        s = self.subs()
        self.assertNotIn("sh", s)       # shampoo is not conditioner
        self.assertNotIn("d4", s)       # diaper size 4 is not size 5

    def test_ambiguous_names_and_flavors(self):
        s = self.subs()
        for k in ("hum1", "hum2", "z1", "z2", "y1", "y2", "gf1", "gf2"):
            self.assertNotIn(k, s, k)

    def test_brand_plus_flavor_only_has_no_kind(self):
        # bug found in the UI test: Fanta grape (soda) was offered for Nevi'ot+ grape (flavored water)
        P = {"fanta": "פאנטה ענבים 1.5 ליטר", "neviot": "נביעות + ענבים 1.5 ליטר"}
        self.assertEqual(c.substitutes(P)[0], {})

    def test_unit_price_guard(self):
        P = {"a": "קפה נמס 200 גרם", "b": "קפה נמס 200 גרם מיוחד"}
        P2 = {"a": "קפה נמס 200 גרם", "b": "קפה נמס 190 גרם"}
        self.assertIn("a", c.substitutes(P2)[0])
        self.assertNotIn("a", c.substitutes(P2, unit_price={"a": 0.1, "b": 0.3})[0])


class Fallback(unittest.TestCase):
    """bug: a store whose price download failed vanished; fallback only covered whole-chain failures.
    Now merge() falls back per store to the previous night, keeping its real date (max 3 days)."""

    def make_prev(self, dated):
        db = sqlite3.connect(":memory:")
        db.executescript(fetch.SCHEMA)
        db.execute("ATTACH DATABASE ':memory:' AS prev")
        # a pre-1.6.0 style table: no source/chain_he columns, club as text, rate in percent, end 'None'
        db.execute("CREATE TABLE prev.stores(sid INTEGER PRIMARY KEY, chain, store_id, name, address, city, items INT, promos INT, stale INT DEFAULT 0, data_date TEXT)")
        for t in ("products", "prices", "promos", "promo_items"):
            ddl = [l for l in fetch.SCHEMA.split(";") if f"CREATE TABLE {t}(" in l][0]
            db.execute(ddl.replace(f"CREATE TABLE {t}(", f"CREATE TABLE prev.{t}("))
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

    def test_failed_store_is_kept_with_its_date_and_normalized(self):
        db = self.make_prev("2026-11-19")
        out = fetch.merge(db, date(2026, 11, 20), {"prev"})
        self.assertEqual(sorted(r["store"] for r in out), ["A", "B"])
        self.assertEqual(set(db.execute("SELECT stale, data_date, source FROM stores").fetchall()), {(1, "2026-11-19", "prev")})
        self.assertEqual(db.execute("SELECT DISTINCT end, club, discount_rate FROM promos").fetchall(), [(None, 1, 0.25)])

    def test_old_fallback_is_dropped(self):  # bug: "yesterday" data was carried forever
        db = self.make_prev("2026-11-19")
        out = fetch.merge(db, date(2026, 11, 20), {"prev"})
        self.assertNotIn("C", [r["store"] for r in out])

    def test_undated_previous_db_counts_as_yesterday(self):
        db = self.make_prev(None)
        out = fetch.merge(db, date(2026, 11, 20), {"prev"})
        self.assertEqual({r["date"] for r in out if r["chain"] == "shufersal"}, {"2026-11-19"})


if __name__ == "__main__":
    unittest.main()
