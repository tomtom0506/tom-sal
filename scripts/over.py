"""Client for the over.org.il price-transparency API (mirrors the chains' official files, no key needed).
Docs: https://www.over.org.il/api  ·  tables: /api/prices/tables
Pure stdlib so it can run and be tested without israeli_prices."""
import csv, io, json, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict

BASE = "https://www.over.org.il/api/prices"
UA = "price-compare-bot/1.0"
PAGE = 1000          # API max rows per page
ID_CHUNK = 150       # promotion ids per request (keeps the URL short)

# over.org.il chain key -> our slug (the israeli_prices slug, so user selections keep working)
SLUG = {
 "shufersal:shufersal": "shufersal", "cerberus:ramilevi": "rami-levy", "cerberus:osherad": "osher-ad",
 "cerberus:doralon": "dor-alon", "cerberus:freshmarket": "fresh-market", "cerberus:paz_bo": "yellow",
 "cerberus:tivtaam": "tiv-taam", "cerberus:yuda_ho": "super-yuda", "cerberus:yohananof": "yochananof",
 "cerberus:keshet": "keshet-teamim", "cerberus:politzer": "politzer", "cerberus:stop_market": "stop-market",
 "cerberus:salachd": "salach-dabach", "bina:goodpharm": "good-pharm", "bina:superbareket": "super-bareket",
 "bina:supersapir": "super-sapir", "bina:citymarketkiryatgat": "city-market", "bina:kingstore": "king-store",
 "bina:maayan2000": "maayan-2000", "bina:shefabirkathashem": "shefa-birkat-hashem", "bina:shuk-hayir": "shuk-hayir",
 "bina:zolvebegadol": "zol-vebegadol", "kt:kt": "meshnat-yosef", "wolt:wolt": "wolt", "carrefour:carrefour": "carrefour",
 "superpharm:superpharm": "super-pharm", "laib:victory": "victory", "laib:mshuk": "mahsanei-hashuk",
 "laib:hcohen": "het-cohen", "hazihinam:hazihinam": "hatzi-hinam",
}
KEY = {v: k for k, v in SLUG.items()}


def slug_of(key):
    return SLUG.get(key) or key.split(":", 1)[-1].replace("_", "-")


def bare(store_id):
    s = str(store_id or "").strip()
    return str(int(s)) if s.isdigit() else s


class Client:
    def __init__(self, base=BASE, tries=4, timeout=90, opener=None):
        self.base, self.tries, self.timeout = base, tries, timeout
        self.opener = opener or urllib.request.urlopen
        self.requests = 0
        self.bytes = 0

    def _get(self, path, params):
        url = f"{self.base}/{path}?{urllib.parse.urlencode(params)}"
        for t in range(self.tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with self.opener(req, timeout=self.timeout) as r:
                    body = r.read()
                self.requests += 1
                self.bytes += len(body)
                return body
            except urllib.error.HTTPError as e:
                # 4xx other than rate limiting will not get better on retry
                if e.code not in (429, 500, 502, 503, 504) or t == self.tries - 1:
                    raise
                wait = int(e.headers.get("Retry-After") or 0) or 10 * (t + 1)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if t == self.tries - 1:
                    raise
                wait = 10 * (t + 1)
            time.sleep(wait)

    def json(self, path, **params):
        return json.loads(self._get(path, params))

    def table(self, name, **params):
        """All current rows of a table (CSV pages of 1000), as dicts of strings."""
        out, off = [], 0
        while True:
            body = self._get(f"table/{name}", {**params, "format": "csv", "limit": PAGE, "offset": off})
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
            out += rows
            if len(rows) < PAGE:
                return out
            off += PAGE


def num(x):
    try:
        return None if x in (None, "") else float(x)
    except ValueError:
        return None


ITEM_COLS = "item_code,item_name,manufacturer_name,quantity,unit_of_measure,is_weighted,item_price,unit_of_measure_price,as_of"
PROMO_COLS = ("promotion_id,description,start_date,end_date,discount_rate,discounted_price,min_qty,"
              "min_purchase_amount,club_id,additional_is_coupon")
PITEM_COLS = "promotion_id,item_code,discounted_price,discount_rate,min_qty,is_gift_item"


def area_stores(c, city_codes):
    return c.table("prices_stores", city_code=",".join(sorted(city_codes)),
                   columns="chain,store_id,store_name,address,city,city_code")


def chain_names(c):
    return {x["chain"]: x.get("account") or x.get("name") for x in c.json("chains").get("chains", [])}


def fetch_store(c, key, store_id):
    """Prices, promotions and promotion items of one store. Returns dict or None when over has no prices for it."""
    sid = bare(store_id)
    items = c.table("prices_market", chain=key, store_id=sid, columns=ITEM_COLS)
    if not items:
        return None
    promos = c.table("prices_promotions", chain=key, store_id=sid, columns=PROMO_COLS)
    ids = [p["promotion_id"] for p in promos]
    pitems = []
    for i in range(0, len(ids), ID_CHUNK):
        pitems += c.table("prices_promotion_items", chain=key, promotion_id=",".join(ids[i:i + ID_CHUNK]), columns=PITEM_COLS)
    meta = c.table("prices_stores", chain=key, store_id=sid, columns="store_name,address,city")
    return {"items": items, "promos": promos, "pitems": pitems, "meta": meta[0] if meta else {},
            "as_of": max((r.get("as_of") or "") for r in items)[:10] or None}


def split_promos(promos, pitems):
    """over.org.il keeps discounted_price / rate / min_qty per item; the promotion row is often empty.
    Split each promotion into one row per distinct (price, rate, min_qty) so every item gets its own terms.
    Coupons and spend-threshold promos are dropped (they are not item prices).
    Yields (promo_row, values, [item_codes])."""
    by = defaultdict(list)
    for it in pitems:
        by[it["promotion_id"]].append(it)
    for p in promos:
        if str(p.get("additional_is_coupon") or "").strip().lower() in ("1", "true", "yes"):
            continue
        if (num(p.get("min_purchase_amount")) or 0) > 0:
            continue
        groups = defaultdict(list)
        for it in by.get(p["promotion_id"], []):
            dp = num(it.get("discounted_price"))
            dr = num(it.get("discount_rate"))
            mq = num(it.get("min_qty"))
            vals = (dp if dp is not None else num(p.get("discounted_price")),
                    dr if dr is not None else num(p.get("discount_rate")),
                    mq if mq is not None else num(p.get("min_qty")))
            groups[vals].append(it["item_code"])
        for vals, codes in groups.items():
            yield p, vals, codes
