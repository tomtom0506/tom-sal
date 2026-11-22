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

    def test_packed_produce_compared_per_kg(self):
        # bug (v1.5): a 500 g pack price was compared to a per-kg price. Now the pack joins the product
        # with its price converted to per kg (x2), never compared as is.
        f = {}
        g = c.group_products([("shufersal:1", "עגבניות שרי ארוז 500 גרם", 0),
                              ("rami-levy:2", "עגבניה שרי במשקל", 1),
                              ("yellow:3", "עגבניה שרי", 1)], factors=f)
        self.assertEqual(g["rami-levy:2"], g["yellow:3"])
        self.assertEqual(g["shufersal:1"], g["rami-levy:2"])
        self.assertEqual(f, {"shufersal:1": 2.0})

    def test_fresh_items_merge_across_chains(self):
        # Tomer: "no fresh salmon, no ground beef, no onion, no lettuce" - each chain names them differently
        rows = [("shufersal:1", "נתחי סלמון טרי שופרסל לק\"ג", 1), ("tiv-taam:2", "פילה סלמון פרוס טרי סקין", 1),
                ("yoh:3", "סלמון טרי", 1), ("x:4", "פילה סלמון קפוא", 1), ("7290000000005", "סלמון מעושן 100 גרם", 0),
                ("a:6", "טחון בקר פרימיום טרי", 1), ("b:7", "בקר טחון טרי", 1), ("c:8", "נתח בשר בקר טחון", 1),
                ("d:9", "בשר טחון טרי", 1), ("e:10", "בשר בקר טחון 1 קג", 0), ("f:11", "עוף טחון", 1),
                ("g:12", "בצל יבש", 1), ("h:13", "בצל", 1), ("i:14", "בצל לבן ישראלי", 1), ("j:15", "בצל ירוק", 0),
                ("k:16", "בצל סגול", 1), ("l:17", "בצל אדום", 1), ("7290000000018", "חסה ערבית", 0), ("7290000000019", "חסה ערבית שקית", 0),
                ("o:20", "חסה עגולה", 0), ("7290000000021", "תפו שמנת בצל 50 גר", 0), ("7290000000022", "מרק בצל 400 גרם", 0)]
        f = {}
        g = c.group_products(rows, factors=f)
        same = lambda *ks: len({g.get(k) for k in ks}) == 1 and g.get(ks[0]) is not None
        self.assertTrue(same("shufersal:1", "tiv-taam:2", "yoh:3"))       # fresh salmon
        self.assertNotEqual(g["x:4"], g["yoh:3"])                          # frozen is not fresh
        self.assertNotIn("7290000000005", g)                               # smoked salmon (barcode): a packaged product
        self.assertTrue(same("a:6", "b:7", "c:8", "d:9", "e:10"))          # ground beef, incl. a 1 kg pack per kg
        self.assertEqual(f["e:10"], 1.0)
        self.assertNotEqual(g["f:11"], g["a:6"])                           # chicken is not beef
        self.assertTrue(same("g:12", "h:13", "i:14"))                      # onion
        self.assertTrue(same("k:16", "l:17"))                              # red = purple onion
        self.assertNotEqual(g["j:15"], g["h:13"])                          # green onion is another vegetable
        self.assertTrue(same("7290000000018", "7290000000019"))                              # lettuce with a barcode, sold per unit
        self.assertNotEqual(g["o:20"], g["7290000000018"])                          # round lettuce is another kind
        self.assertNotIn("7290000000021", g); self.assertNotIn("7290000000022", g)  # onion snacks / soup stay products

    def test_suppliers_ignored_and_guards(self):
        rows = [("1", "חזה עוף טרי עטרה הרב", 1), ("2", "חזה עוף טרי ארוז שלי קג", 1), ("3", "חזה עוף טרי מחפוד אר", 1),
                ("4", "טחון מהצומח 500ג טבע דלי", 0), ("5", "בשר טחון טרי", 1), ("6", "בשר טחון מקפוא עטרה", 1),
                ("7", "חזה הודו טרי", 1), ("8", "חזה הודו טרי ארוז", 1), ("9", "חזה הודו גולן", 1)]
        g = c.group_products([(f"729000000000{k}", n, w) for k, n, w in rows],
                             prices={"7290000000007": 40, "7290000000008": 42, "7290000000009": 110})
        k = lambda i: g.get(f"729000000000{i}")
        self.assertTrue(k(1) == k(2) == k(3) == "חזה עוף")        # supplier names do not split a product
        self.assertNotEqual(k(4), k(5))                              # plant-based "ground" is not beef
        self.assertNotEqual(k(6), k(5))                              # frozen (any spelling) is not fresh
        self.assertEqual(k(7), k(8))
        self.assertNotEqual(k(9), k(7))                              # smoked deli turkey at 110/kg split by the price guard

    def test_white_gourmet_potato_is_potato(self):
        self.assertEqual(c.produce_key("תפוח אדמה לבן גורמה")[0], c.produce_key("תפוח אדמה קטן לאפיה")[0])

    def test_potato_vs_apple_and_cut_off_sizes(self):
        # from Tomer's table: Osher Ad potatoes ("תפוח אדמה", singular) and ground beef "1.2 ק"" were not matched
        self.assertEqual(c.produce_key("תפוח אדמה תפזורת")[0], c.produce_key("תפוא לבן ישראל")[0].replace(" לבן", ""))
        self.assertNotEqual(c.produce_key("תפוח עץ סמיט")[0], c.produce_key("תפוח אדמה")[0])
        self.assertEqual(c.size_tag('בקר טחון עטרה 1.2 ק"'), '1.2 ק"ג')
        f = {}
        g = c.group_products([("7290000000001", 'בקר טחון עטרה 1.2 ק"', 0), ("7290000000002", "בקר טחון טרי", 1)], factors=f)
        self.assertEqual(g["7290000000001"], g["7290000000002"])
        self.assertAlmostEqual(f["7290000000001"], 1000 / 1200)
        # frozen salmon whose name the chain cut off ("קפ") is not fresh salmon
        self.assertEqual(c.produce_key("פילה סלמון נורבגי קפ")[0], c.produce_key("פילה סלמון קפוא")[0])

    def test_aliases_for_saved_baskets(self):
        al = {}
        c.group_products([("a:1", "בצל יבש", 1), ("b:2", "בצל", 1)], aliases=al)
        self.assertEqual(al.get("בצל יבש"), "בצל")

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

    def test_cut_off_milk_and_fillers(self):
        # Tomer: Yochananof "has no 3% milk" - the basket item's name was cut off before "ליטר"
        P = {"tnuva": "חלב תנובה 3% שומן 1", "tara": "טרה חלב 3% 1למהדרין", "one": "חלב 1% 1 ליטר"}
        s = c.substitutes(P, units={"tnuva": "100 ליטר"})[0]
        self.assertEqual([d for d, _ in s["tnuva"]], ["tara"])
        # the trailing number of a name without % may be the % itself: "שקית 3" is not 3 litres
        self.assertIsNone(c.product_features("חלב תנובה טרי שקית 3", "ליטר")["size"])
        # the unit field may state the pack size when the name does not
        self.assertEqual(c.product_features("חלב 3% ועדת מהדרין בקרטון", "1 ליטר")["size"], 1000)
        self.assertIsNone(c.product_features("קפה נמס", "100 גרם")["size"])     # "100 גרם" is the unit-price basis

    def test_flavor_and_size_words_from_live_sample(self):
        P = {"a": "משקה סויה שוקו 1 ליטר", "b": "משקה סויה 1 ליטר", "c": "צלחות קטן 50 יחידות", "d": "צלחות גדול 50 יח",
             "e": "תחליב רחצה אלוורה 750 מל", "f": "תחליב רחצה ווניל 750 מל"}
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
