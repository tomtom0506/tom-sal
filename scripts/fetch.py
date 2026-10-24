"""Nightly fetch: prices + promos for stores in the user's area -> out/sal.db"""
import json, os, re, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import israeli_prices as ilp

# Area: Kiryat Ono + neighbours. Names and CBS settlement codes.
AREA_NAMES = ["קרית אונו", "גני תקווה", "סביון", "יהוד", "אור יהודה"]
AREA_CODES = {"2620", "229", "587", "9400", "2400"}
OUT = Path("out"); OUT.mkdir(exist_ok=True)

def norm(s):
 s = (s or "").replace("קריית", "קרית").replace("תקוה", "תקווה")
 return re.sub(r"[\"'׳״\-]", " ", s)

def in_area(st):
 if (st.city or "").strip() in AREA_CODES:
  return True
 text = norm(f"{st.city} {st.address} {st.name}")
 return any(n in text for n in AREA_NAMES)

def get_stores(slug):
 try:
  return ilp.get_stores(slug)
 except ilp.FileNotFound:
  if slug != "super-pharm":
   raise
 # Super-Pharm: stores file is listed under a different category name
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
 db.executescript("""
 CREATE TABLE stores(chain, store_id, name, address, city, items INT, promos INT);
 CREATE TABLE prices(chain, store_id, item_code, name, manufacturer, quantity REAL,
 unit, is_weighted INT, price REAL, unit_price REAL, updated TEXT);
 CREATE TABLE promos(chain, store_id, promotion_id, description, start TEXT, end TEXT,
 club TEXT, min_qty REAL, discounted_price REAL, discount_rate REAL, item_code);
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
   db.executemany("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
    (chain, st.store_id, i.item_code, i.name, i.manufacturer, d(i.quantity),
    i.unit_of_measure, int(bool(i.is_weighted)), d(i.price), d(i.unit_price),
    str(i.price_update_time)) for i in items])
   prow = []
   for p in promos:
    for it in p.items:
     prow.append((chain, st.store_id, p.promotion_id, p.description, str(p.start_time),
        str(p.end_time), p.club_id, d(p.min_qty or it.min_qty),
        d(p.discounted_price or it.discounted_price),
        d(p.discount_rate or it.discount_rate), it.item_code))
   db.executemany("INSERT INTO promos VALUES(?,?,?,?,?,?,?,?,?,?,?)", prow)
   db.execute("INSERT INTO stores VALUES(?,?,?,?,?,?,?)",
     (chain, st.store_id, st.name, st.address, st.city, len(items), len(promos)))
   rows.append({"chain": chain, "he": chains[chain].name_he, "store": st.name,
      "city": st.city, "items": len(items), "promos": len(promos)})
   print("OK", chain, st.name, len(items), len(promos))
 db.executescript("CREATE INDEX ip ON prices(item_code); CREATE INDEX iq ON promos(item_code);")
 db.commit(); db.close()

 (OUT / "summary.json").write_text(json.dumps({"run_at": datetime.now(timezone.utc).isoformat(),
  "stores": rows, "errors": errors}, ensure_ascii=False, indent=1), encoding="utf-8")
 mb = round(db_path.stat().st_size / 1e6, 1)
 s = os.environ.get("GITHUB_STEP_SUMMARY")
 if s:
  L = [f"## Stores in area: {len(rows)} | DB: {mb}MB", "",
   "| chain | store | city | items | promos |", "|---|---|---|---|---|"]
  L += [f"| {r['he']} | {r['store']} | {r['city']} | {r['items']} | {r['promos']} |" for r in rows]
  if errors:
   L += ["", f"### Errors ({len(errors)})", ""] + [f"- {e['chain']} {e.get('store','')}: {e['error']}" for e in errors]
  Path(s).write_text("\n".join(L), encoding="utf-8")

if __name__ == "__main__":
 main()
