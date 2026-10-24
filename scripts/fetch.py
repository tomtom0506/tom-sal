"""Nightly fetch: prices + promos for stores in the user's area -> out/sal.db.gz"""
import gzip, json, os, re, shutil, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import israeli_prices as ilp

AREA_NAMES = ["קרית אונו", "גני תקווה", "סביון", "יהוד", "אור יהודה"]
AREA_CODES = {"2620", "229", "587", "9400", "2400"}
OUT = Path("out"); OUT.mkdir(exist_ok=True)

def norm(s):
 s = (s or "").replace("קריית", "קרית").replace("תקוה", "תקווה")
 return re.sub(r"[\"'׳״\-]", " ", s)

# Whole-word match only: "יהוד" must not match "יהודה" (בן יהודה, מחנה יהודה...)
AREA_RE = re.compile(r"(?<![א-ת])(" + "|".join(AREA_NAMES) + r")(?![א-ת])")

def in_area(st):
 city = (st.city or "").strip()
 if city.isdigit() and city != "0":
  return city in AREA_CODES
 return bool(AREA_RE.search(norm(f"{city} {st.address} {st.name}")))

def get_stores(slug):
 try:
  return ilp.get_stores(slug)
 except ilp.FileNotFound:
  if slug != "super-pharm":
   raise
 from israeli_prices.chains import superpharm as sp
 ad = ilp.get_adapter(slug)
 for cat in ["Stores", "StoresFull", "Store", "StoreFull", "store"]:
  r = ad.client.get(sp.BASE_URL, params={"Category-equals": cat, "page": 1})
  links = sp._DOWNLOAD_LINK_RE.findall(r.text)
  if links:
   return ilp.parse(ad.client.get(sp.BASE_URL + sp.html.unescape(links[0])).content)
 r = ad.client.get(sp.BASE_URL, params={"page": 1})
 names = {l.split("?")[0].rsplit("/", 1)[-1][:12] for l in sp._DOWNLOAD_LINK_RE.findall(r.text)}
 raise RuntimeError(f"no stores file; prefixes seen: {sorted(names)}")

def d(x):
 return None if x is None else float(x)

def fetch_store(chain, st):
 p = ilp.get_prices(chain, st.store_id)
 try:
  pr = ilp.get_promos(chain, st.store_id).promotions
 except Exception:
  pr = []
 return chain, st, p.items, pr

def main():
 db_path = OUT / "sal.db"
 if db_path.exists(): db_path.unlink()
 db = sqlite3.connect(db_path)
 # Normalized: product details stored once, promo details once per promo.
 db.executescript("""
 CREATE TABLE stores(sid INTEGER PRIMARY KEY, chain, store_id, name, address, city, items INT, promos INT);
 CREATE TABLE products(item_code TEXT PRIMARY KEY, name, manufacturer, quantity REAL,
  unit, is_weighted INT) WITHOUT ROWID;
 CREATE TABLE prices(item_code TEXT, sid INT, price REAL, unit_price REAL,
  PRIMARY KEY(item_code, sid)) WITHOUT ROWID;
 CREATE TABLE promos(pid INTEGER PRIMARY KEY, sid INT, promotion_id, description,
  start TEXT, end TEXT, club TEXT, min_qty REAL, discounted_price REAL, discount_rate REAL);
 CREATE TABLE promo_items(item_code TEXT, pid INT, PRIMARY KEY(item_code, pid)) WITHOUT ROWID;
 """)
 chains = {c.slug: c for c in ilp.list_chains() if c.implemented}
 jobs, errors = [], []
 for slug in chains:
  try:
   for st in get_stores(slug).stores:
    if in_area(st): jobs.append((slug, st))
  except Exception as e:
   errors.append({"chain": slug, "stage": "stores", "error": f"{type(e).__name__}: {str(e)[:120]}"})
 print(f"{len(jobs)} stores in area")

 rows = []
 with ThreadPoolExecutor(max_workers=6) as pool:
  futs = [pool.submit(fetch_store, c, s) for c, s in jobs]
  for (c, s), f in zip(jobs, futs):
   try:
    chain, st, items, promos = f.result(timeout=300)
   except Exception as e:
    errors.append({"chain": c, "store": s.store_id, "stage": "prices", "error": f"{type(e).__name__}: {str(e)[:120]}"})
    continue
   sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos) VALUES(?,?,?,?,?,?,?)",
     (chain, st.store_id, st.name, st.address, st.city, len(items), len(promos))).lastrowid
   db.executemany("INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)", [
    (i.item_code, i.name, i.manufacturer, d(i.quantity), i.unit_of_measure,
     int(bool(i.is_weighted))) for i in items])
   db.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?)", [
    (i.item_code, sid, d(i.price), d(i.unit_price)) for i in items])
   for p in promos:
    it0 = p.items[0] if p.items else None
    g = lambda a: getattr(p, a, None) or (getattr(it0, a, None) if it0 else None)
    pid = db.execute("INSERT INTO promos(sid,promotion_id,description,start,end,club,min_qty,"
      "discounted_price,discount_rate) VALUES(?,?,?,?,?,?,?,?,?)",
      (sid, p.promotion_id, p.description, str(p.start_time), str(p.end_time), p.club_id,
       d(g("min_qty")), d(g("discounted_price")), d(g("discount_rate")))).lastrowid
    db.executemany("INSERT OR IGNORE INTO promo_items VALUES(?,?)", [(it.item_code, pid) for it in p.items])
   rows.append({"chain": chain, "he": chains[chain].name_he, "store": st.name,
      "city": st.city, "items": len(items), "promos": len(promos)})
   print("OK", chain, st.name, len(items), len(promos))
 db.commit()
 db.execute("VACUUM"); db.close()

 gz = OUT / "sal.db.gz"
 with open(db_path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
  shutil.copyfileobj(src, dst)
 raw, packed = round(db_path.stat().st_size / 1e6, 1), round(gz.stat().st_size / 1e6, 1)

 (OUT / "summary.json").write_text(json.dumps({"run_at": datetime.now(timezone.utc).isoformat(),
  "db_mb": raw, "gz_mb": packed, "stores": rows, "errors": errors},
  ensure_ascii=False, indent=1), encoding="utf-8")
 s = os.environ.get("GITHUB_STEP_SUMMARY")
 if s:
  L = [f"## Stores in area: {len(rows)} | DB: {raw}MB | gz: {packed}MB", "",
   "| chain | store | city | items | promos |", "|---|---|---|---|---|"]
  c = lambda x: str(x).replace("|", "/")
  L += [f"| {c(r['he'])} | {c(r['store'])} | {c(r['city'])} | {r['items']} | {r['promos']} |" for r in rows]
  if errors:
   L += ["", f"### Errors ({len(errors)})", ""] + [f"- {e['chain']} {e.get('store','')}: {e['error']}" for e in errors]
  Path(s).write_text("\n".join(L), encoding="utf-8")

if __name__ == "__main__":
 main()
