"""over.org.il client, merge priority and source comparison. Run: python -m unittest discover -s tests"""
import io, sqlite3, sys, types, unittest, urllib.error
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.modules.setdefault("israeli_prices", types.SimpleNamespace(FileNotFound=Exception))
import over, fetch


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def csv_body(header, rows):
    lines = [",".join(header)] + [",".join(str(r.get(h, "")) for h in header) for r in rows]
    return ("\n".join(lines) + "\n").encode()


class ClientTests(unittest.TestCase):
    def test_paging_until_short_page(self):
        calls = []
        def opener(req, timeout):
            off = int(req.full_url.split("offset=")[1].split("&")[0])
            calls.append(off)
            n = over.PAGE if off < 2 * over.PAGE else 7
            return FakeResp(csv_body(["item_code"], [{"item_code": i} for i in range(n)]))
        rows = over.Client(opener=opener).table("prices_market", chain="x")
        self.assertEqual(len(rows), 2 * over.PAGE + 7)
        self.assertEqual(calls, [0, 1000, 2000])

    def test_retries_on_429_then_gives_up_on_404(self):
        n = {"c": 0}
        def opener(req, timeout):
            n["c"] += 1
            if n["c"] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, None)
            return FakeResp(b'{"chains": []}')
        over.time.sleep = lambda s: None
        self.assertEqual(over.Client(opener=opener).json("chains"), {"chains": []})
        def opener404(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            over.Client(opener=opener404).json("chains")

    def test_slug_mapping_keeps_existing_ids(self):
        self.assertEqual(over.slug_of("cerberus:paz_bo"), "yellow")
        self.assertEqual(over.slug_of("bina:citymarketkiryatgat"), "city-market")
        self.assertEqual(over.slug_of("superpharm:superpharm"), "super-pharm")
        self.assertEqual(over.slug_of("new:some_chain"), "some-chain")
        self.assertEqual(over.bare("045"), "45")
        self.assertEqual(over.KEY["shufersal"], "shufersal:shufersal")


class SplitPromos(unittest.TestCase):
    def test_item_level_terms_and_coupon_filter(self):
        promos = [{"promotion_id": "1", "description": "2 ב 20", "discounted_price": "", "discount_rate": "", "min_qty": "",
                   "additional_is_coupon": "0", "min_purchase_amount": ""},
                  {"promotion_id": "2", "description": "קופון", "additional_is_coupon": "1"},
                  {"promotion_id": "3", "description": "מעל 200", "additional_is_coupon": "0", "min_purchase_amount": "200"}]
        pitems = [{"promotion_id": "1", "item_code": "a", "discounted_price": "20", "discount_rate": "28", "min_qty": "2"},
                  {"promotion_id": "1", "item_code": "b", "discounted_price": "20", "discount_rate": "28", "min_qty": "2"},
                  {"promotion_id": "1", "item_code": "c", "discounted_price": "24", "discount_rate": "", "min_qty": "2"},
                  {"promotion_id": "2", "item_code": "a", "discounted_price": "0"}]
        out = sorted((v, sorted(c)) for _, v, c in over.split_promos(promos, pitems))
        self.assertEqual(out, [((20.0, 28.0, 2.0), ["a", "b"]), ((24.0, None, 2.0), ["c"])])


def make_db(rows):
    """rows: [(chain, store_id, data_date, {item: price})] -> in-memory DB with fetch.SCHEMA."""
    db = sqlite3.connect(":memory:")
    db.executescript(fetch.SCHEMA)
    for chain, st, dd, prices in rows:
        sid = db.execute("INSERT INTO stores(chain,store_id,name,items,promos,data_date) VALUES(?,?,?,?,0,?)",
                         (chain, st, f"{chain} {st}", len(prices), dd)).lastrowid
        for code, p in prices.items():
            db.execute("INSERT OR IGNORE INTO products(item_code,name) VALUES(?,?)", (code, code))
            db.execute("INSERT INTO prices VALUES(?,?,?,NULL)", (code, sid, p))
    return db


def attach(main, name, src):
    main.execute(f"ATTACH DATABASE ':memory:' AS {name}")
    main.executescript(fetch.SCHEMA.replace("CREATE TABLE ", f"CREATE TABLE {name}."))
    for t in ("stores", "products", "prices", "promos", "promo_items"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if rows:
            main.executemany(f"INSERT INTO {name}.{t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", rows)


class MergeAndCompare(unittest.TestCase):
    T = date(2026, 11, 21)

    def build(self):
        db = sqlite3.connect(":memory:")
        db.executescript(fetch.SCHEMA)
        attach(db, "ov", make_db([("shufersal", "93", "2026-11-21", {"x": 10, "y": 5}),
                                  ("carrefour", "701", "2026-11-21", {"x": 9}),
                                  ("city-market", "45", "2026-11-19", {"x": 8})]))
        attach(db, "off", make_db([("shufersal", "93", "2026-11-21", {"x": 10, "y": 5.5, "z": 3}),
                                   ("city-market", "045", "2026-11-21", {"x": 8.5}),
                                   ("good-pharm", "070", "2026-11-21", {"x": 7})]))
        attach(db, "prev", make_db([("yellow", "257", "2026-11-20", {"x": 6}),
                                    ("tiv-taam", "021", "2026-11-15", {"x": 6})]))
        return db

    def test_merge_priority(self):
        db = self.build()
        rows = {(r["chain"]): r for r in fetch.merge(db, self.T, {"ov", "off", "prev"})}
        self.assertEqual(rows["shufersal"]["source"], "over")       # both fresh: over wins
        self.assertEqual(rows["carrefour"]["source"], "over")       # only in over
        self.assertEqual(rows["city-market"]["source"], "official") # official is fresher than over
        self.assertEqual(rows["good-pharm"]["source"], "official")  # only in official
        self.assertEqual(rows["yellow"]["source"], "prev")          # neither source today
        self.assertNotIn("tiv-taam", rows)                          # previous data too old
        # official spelling of the store id is kept, so saved selections in the site stay valid
        self.assertEqual(db.execute("SELECT store_id FROM stores WHERE chain='city-market'").fetchone()[0], "045")
        self.assertEqual(db.execute("SELECT stale FROM stores WHERE chain='yellow'").fetchone()[0], 1)

    def test_compare_counts(self):
        cmp = fetch.compare(self.build())
        t = cmp["total"]
        self.assertEqual(t["stores"], 2)           # shufersal-93 and city-market-45
        self.assertEqual(t["both"], 3)
        self.assertEqual(t["equal"], 1)            # shufersal x
        self.assertEqual(t["only_official"], 1)    # shufersal z
        self.assertEqual(t["only_over_stores"], ["carrefour-701"])
        self.assertEqual(t["only_official_stores"], ["good-pharm-70"])
        sh = next(s for s in cmp["stores"] if s["chain"] == "shufersal")
        self.assertEqual(sh["diffs"][0]["code"], "y")


class InsertOver(unittest.TestCase):
    def test_insert_over_store(self):
        db = sqlite3.connect(":memory:")
        db.executescript(fetch.SCHEMA)
        data = {"meta": {"store_name": "קרפור סיטי", "address": "הנשיא 57", "city": "קריית אונו"}, "as_of": "2026-11-21",
                "items": [{"item_code": "7290004131074", "item_name": "חלב", "item_price": "7.35", "unit_of_measure_price": "7.35",
                           "is_weighted": "0", "quantity": "1"},
                          {"item_code": "123", "item_name": "עגבניה", "item_price": "5.9", "is_weighted": "1"}],
                "promos": [{"promotion_id": "9", "description": "2 ב 12", "start_date": "2026-11-01", "end_date": "2026-11-28 02:59",
                            "club_id": "0 - כלל הלקוחות", "additional_is_coupon": "0"}],
                "pitems": [{"promotion_id": "9", "item_code": "7290004131074", "discounted_price": "12", "discount_rate": "18", "min_qty": "2"}]}
        fetch.insert_over(db, "carrefour", "701", data, "קרפור")
        self.assertEqual(db.execute("SELECT chain, store_id, source, data_date, promos FROM stores").fetchone(),
                         ("carrefour", "701", "over", "2026-11-21", 1))
        self.assertEqual(sorted(r[0] for r in db.execute("SELECT item_code FROM prices")), ["7290004131074", "carrefour:123"])
        self.assertEqual(db.execute("SELECT is_weighted FROM products WHERE item_code='carrefour:123'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT club, min_qty, discounted_price, discount_rate, end FROM promos").fetchone(),
                         (0, 2.0, 12.0, 0.18, "2026-11-28 02:59"))


if __name__ == "__main__":
    unittest.main()
