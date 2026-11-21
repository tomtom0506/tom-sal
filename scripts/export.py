"""Export slim JSON files for the web app: out/sal.db -> site/data/"""
import json, sqlite3, sys
from pathlib import Path
import israeli_prices as ilp
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (today_il, group_products, is_coupon, promo_active, norm_date, norm_rate, norm_club, norm_qty)

DATA = Path("site/data"); (DATA / "s").mkdir(parents=True, exist_ok=True)


def dump(path, obj):
 path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def r2(x):
 return None if x is None else round(x, 4)


def main():
 he = {c.slug: c.name_he for c in ilp.list_chains()}
 db = sqlite3.connect("out/sal.db")
 today = today_il().isoformat()
 tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 meta = dict(db.execute("SELECT k, v FROM meta").fetchall()) if "meta" in tables else {}
 cols = [r[1] for r in db.execute("PRAGMA table_info(stores)")]
 stale_col = "stale" if "stale" in cols else "0"
 date_col = "data_date" if "data_date" in cols else "NULL"

 # 1) catalog + cross-chain groups (produce, deli, eggs)
 prod = db.execute("SELECT item_code, name, manufacturer, quantity, unit, is_weighted FROM products").fetchall()
 code2group = group_products((c, n, w) for c, n, m, q, u, w in prod)
 cat, groups = {}, {}
 for c, n, m, q, u, w in prod:
  g = code2group.get(c)
  if g:
   egg = g.startswith("ביצים")
   groups.setdefault(g, {"u": "" if egg else u, "w": 0 if egg else int(bool(w)), "c": []})["c"].append(c)
  else:
   cat[c] = [n, m, q, u]
 for g, v in groups.items():
  cat["g:" + g] = [g, "", None, v["u"], v["w"], v["c"]]

 # 2) price sanity inside groups: a price far from the group median is almost always a unit error
 #    (per unit vs per kg, per 100g) - drop it rather than show a wrong cheapest store
 gp = {}
 for c, p in db.execute("SELECT item_code, price FROM prices"):
  if c in code2group and p:
   gp.setdefault(code2group[c], []).append(p)
 med = {g: sorted(v)[len(v) // 2] for g, v in gp.items() if len(v) >= 3}
 dropped = 0

 # 3) per-store files. promo row: [description, end, club(0=everyone), min_qty, discounted_price, rate(0..1), start]
 links = {}
 for code, pid, sid in db.execute("SELECT pi.item_code, pi.pid, p.sid FROM promo_items pi JOIN promos p ON p.pid = pi.pid"):
  links.setdefault(sid, {}).setdefault(code, []).append(pid)
 stores, dates = [], set()
 he_col = "chain_he" if "chain_he" in cols else "NULL"
 src_col = "source" if "source" in cols else "NULL"
 for sid, chain, store_id, name, address, stale, ddate, chain_he, src in db.execute(
   f"SELECT sid, chain, store_id, name, address, {stale_col}, {date_col}, {he_col}, {src_col} FROM stores ORDER BY chain, name").fetchall():
  key = f"{chain}-{store_id}"  # stable across nightly runs (sid is not)
  ddate = (ddate or meta.get("data_date") or "")[:10] or None
  dates.add(ddate)
  stores.append({"sid": key, "chain": chain, "he": he.get(chain) or chain_he or chain, "name": name,
   "address": address, "stale": int(stale or 0), "date": ddate, "src": src})
  prices = {}
  for c, p in db.execute("SELECT item_code, price FROM prices WHERE sid=?", (sid,)):
   if not p or p <= 0:
    continue
   g = code2group.get(c)
   if g in med and not (0.4 * med[g] <= p <= 3 * med[g]):
    dropped += 1
    continue
   prices[c] = p
  promos = {}
  for pid, desc, start, end, club, mq, dp, dr in db.execute(
    "SELECT pid, description, start, end, club, min_qty, discounted_price, discount_rate FROM promos WHERE sid=?", (sid,)):
   start, end, rate, mq = norm_date(start), norm_date(end), norm_rate(dr), norm_qty(mq)
   if not promo_active(start, end, today):
    continue  # expired, or not started yet
   if is_coupon(desc, mq, rate):
    continue  # basket coupons / spend thresholds / gifts are not item prices
   promos[pid] = [desc, (end or "")[:10], norm_club(club), mq, dp, r2(rate), (start or "")[:10]]
  l = {c: [p for p in ps if p in promos] for c, ps in links.get(sid, {}).items() if c in prices}
  dump(DATA / "s" / f"{key}.json", {"p": prices, "pr": promos, "l": {c: ps for c, ps in l.items() if ps}})
 data_date = meta.get("data_date") or max((d for d in dates if d), default=today)
 dump(DATA / "stores.json", {"updated": data_date, "run_at": meta.get("run_at"), "exported": today, "stores": stores})
 dump(DATA / "catalog.json", cat)
 print(len(stores), "stores,", len(cat), "catalog entries,", len(groups), "groups,", dropped, "outlier prices dropped")


if __name__ == "__main__":
 main()
