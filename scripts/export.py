"""Export slim JSON files for the web app: out/sal.db -> site/data/"""
import json, sqlite3
from datetime import date
from pathlib import Path
import israeli_prices as ilp

DATA = Path("site/data"); (DATA / "s").mkdir(parents=True, exist_ok=True)

def dump(path, obj):
 path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def main():
 he = {c.slug: c.name_he for c in ilp.list_chains()}
 db = sqlite3.connect("out/sal.db")
 today = date.today().isoformat()
 # promo -> items, once for all stores
 links = {}
 for code, pid, sid in db.execute("SELECT pi.item_code, pi.pid, p.sid FROM promo_items pi JOIN promos p ON p.pid = pi.pid"):
  links.setdefault(sid, {}).setdefault(code, []).append(pid)
 stores = []
 for sid, chain, store_id, name, address, city, items in db.execute(
   "SELECT sid, chain, store_id, name, address, city, items FROM stores ORDER BY chain, name").fetchall():
  key = f"{chain}-{store_id}"  # stable across nightly runs (sid is not)
  stores.append({"sid": key, "chain": chain, "he": he.get(chain, chain), "name": name,
   "address": address, "city": city, "items": items})
  prices = {c: [p, u] for c, p, u in db.execute("SELECT item_code, price, unit_price FROM prices WHERE sid=?", (sid,))}
  promos = {}
  for pid, desc, end, club, mq, dp, dr in db.execute(
    "SELECT pid, description, end, club, min_qty, discounted_price, discount_rate FROM promos WHERE sid=?", (sid,)):
   end = end or ""
   if end[:4].isdigit() and end[:10] < today:
    continue  # expired
   promos[pid] = [desc, end[:10], club, mq, dp, dr]
  l = {c: [p for p in ps if p in promos] for c, ps in links.get(sid, {}).items()}
  dump(DATA / "s" / f"{key}.json", {"p": prices, "pr": promos, "l": {c: ps for c, ps in l.items() if ps}})
 dump(DATA / "stores.json", {"updated": today, "stores": stores})
 cat = {c: [n, m, q, u] for c, n, m, q, u in db.execute("SELECT item_code, name, manufacturer, quantity, unit FROM products")}
 dump(DATA / "catalog.json", cat)
 print(len(stores), "stores,", len(cat), "products")

if __name__ == "__main__":
 main()
