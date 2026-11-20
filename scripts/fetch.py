"""Nightly fetch: prices + promos for stores in the user's area -> out/sal.db.gz"""
import gzip, json, os, re, shutil, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
import israeli_prices as ilp
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import key, norm_date, norm_rate, norm_club, norm_qty, today_il, fresh_enough, publish_ok

RUN_BUDGET = 25 * 60  # seconds for all price downloads; stuck stores fall back to their previous data

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

def get_stores(slug, tries=3):
 # portals fail transiently (e.g. Shufersal on 30.10): retry before giving up
 for t in range(tries):
  try:
   return _get_stores(slug)
  except Exception:
   if t == tries - 1:
    raise
   time.sleep(20 * (t + 1))

def _get_stores(slug):
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

SCHEMA = """
 CREATE TABLE meta(k TEXT PRIMARY KEY, v);
 CREATE TABLE stores(sid INTEGER PRIMARY KEY, chain, store_id, name, address, city, items INT, promos INT,
  stale INT DEFAULT 0, data_date TEXT);
 CREATE TABLE products(item_code TEXT PRIMARY KEY, name, manufacturer, quantity REAL,
  unit, is_weighted INT) WITHOUT ROWID;
 CREATE TABLE prices(item_code TEXT, sid INT, price REAL, unit_price REAL,
  PRIMARY KEY(item_code, sid)) WITHOUT ROWID;
 CREATE TABLE promos(pid INTEGER PRIMARY KEY, sid INT, promotion_id, description,
  start TEXT, end TEXT, club INT, min_qty REAL, discounted_price REAL, discount_rate REAL);
 CREATE TABLE promo_items(item_code TEXT, pid INT, PRIMARY KEY(item_code, pid)) WITHOUT ROWID;
"""


def insert_store(db, chain, st, items, promos, today):
 sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos,data_date) VALUES(?,?,?,?,?,?,?,?)",
   (chain, str(st.store_id), st.name, st.address, st.city, len(items), len(promos), today)).lastrowid
 db.executemany("INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)", [
  (key(chain, i.item_code), i.name, i.manufacturer, d(i.quantity), i.unit_of_measure,
   int(bool(i.is_weighted))) for i in items])
 db.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?)", [
  (key(chain, i.item_code), sid, d(i.price), d(i.unit_price)) for i in items])
 for p in promos:
  it0 = p.items[0] if p.items else None
  g = lambda a: getattr(p, a, None) or (getattr(it0, a, None) if it0 else None)
  pid = db.execute("INSERT INTO promos(sid,promotion_id,description,start,end,club,min_qty,"
    "discounted_price,discount_rate) VALUES(?,?,?,?,?,?,?,?,?)",
    (sid, p.promotion_id, p.description, norm_date(p.start_time), norm_date(p.end_time), norm_club(p.club_id),
     norm_qty(g("min_qty")), d(g("discounted_price")), norm_rate(g("discount_rate")))).lastrowid
  db.executemany("INSERT OR IGNORE INTO promo_items VALUES(?,?)", [(key(chain, it.item_code), pid) for it in p.items])


def copy_prev(db, today, failed_chains, failed_stores, have):
 """Copy yesterday's rows for every store that failed today - a whole chain (stage 'stores') or a single
 store (stage 'prices') - as long as that data is at most MAX_STALE_DAYS old. Returns rows for the summary."""
 out = []
 cols = [r[1] for r in db.execute("PRAGMA prev.table_info(stores)")]
 if not cols:
  return out
 date_col = "data_date" if "data_date" in cols else "NULL"
 old = db.execute(f"SELECT sid, chain, store_id, name, address, city, items, promos, {date_col} FROM prev.stores").fetchall()
 for osid, chain, store_id, name, address, city, items, promos, ddate in old:
  k = (chain, str(store_id))
  if k in have or not (chain in failed_chains or k in failed_stores):
   continue
  # DBs from before v1.6.0 carry no date: they are at most one night old
  ddate = ddate or (today.fromordinal(today.toordinal() - 1)).isoformat()
  if not fresh_enough(ddate, today):
   print("too old, dropped:", chain, name, ddate)
   continue
  sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos,stale,data_date) VALUES(?,?,?,?,?,?,?,1,?)",
    (chain, str(store_id), name, address, city, items, promos, ddate)).lastrowid
  db.execute("INSERT OR IGNORE INTO products SELECT pr.* FROM prev.products pr JOIN prev.prices p ON p.item_code=pr.item_code WHERE p.sid=?", (osid,))
  db.execute("INSERT OR REPLACE INTO prices SELECT item_code, ?, price, unit_price FROM prev.prices WHERE sid=?", (sid, osid))
  for opid, *rest in db.execute("SELECT pid, promotion_id, description, start, end, club, min_qty, discounted_price, discount_rate FROM prev.promos WHERE sid=?", (osid,)).fetchall():
   pr_id, desc, start, end, club, mq, dp, dr = rest
   pid = db.execute("INSERT INTO promos(sid,promotion_id,description,start,end,club,min_qty,discounted_price,discount_rate) VALUES(?,?,?,?,?,?,?,?,?)",
     (sid, pr_id, desc, norm_date(start), norm_date(end), norm_club(club), norm_qty(mq), dp, norm_rate(dr))).lastrowid
   db.execute("INSERT OR IGNORE INTO promo_items SELECT item_code, ? FROM prev.promo_items WHERE pid=?", (pid, opid))
  have.add(k)
  out.append({"chain": chain, "store": name, "city": city, "items": items, "promos": promos, "stale": ddate})
 return out


def main():
 today = today_il()
 db_path = OUT / "sal.db"
 if db_path.exists(): db_path.unlink()
 db = sqlite3.connect(db_path)
 db.executescript(SCHEMA)
 chains = {c.slug: c for c in ilp.list_chains() if c.implemented}
 jobs, errors = [], []
 for slug in chains:
  try:
   for st in get_stores(slug).stores:
    if in_area(st): jobs.append((slug, st))
  except Exception as e:
   errors.append({"chain": slug, "stage": "stores", "error": f"{type(e).__name__}: {str(e)[:120]}"})
 print(f"{len(jobs)} stores in area")

 rows, have = [], set()
 # No `with`: leaving a with-block waits for stuck threads, so one hung store would kill the whole night.
 pool = ThreadPoolExecutor(max_workers=6)
 futs = {pool.submit(fetch_store, c, s): (c, s) for c, s in jobs}
 done, not_done = wait(futs, timeout=RUN_BUDGET)
 for f in not_done:
  c, s = futs[f]
  errors.append({"chain": c, "store": str(s.store_id), "stage": "prices", "error": f"timeout after {RUN_BUDGET}s"})
 pool.shutdown(wait=False, cancel_futures=True)
 for f, (c, s) in futs.items():
  if f not in done:
   continue
  try:
   chain, st, items, promos = f.result()
  except Exception as e:
   errors.append({"chain": c, "store": str(s.store_id), "stage": "prices", "error": f"{type(e).__name__}: {str(e)[:120]}"})
   continue
  insert_store(db, chain, st, items, promos, today.isoformat())
  have.add((chain, str(st.store_id)))
  rows.append({"chain": chain, "he": chains[chain].name_he, "store": st.name,
     "city": st.city, "items": len(items), "promos": len(promos)})
  print("OK", chain, st.name, len(items), len(promos))
 db.commit()
 fresh = len(rows)

 # failed today (whole chain or single store): keep the previous data, marked stale with its real date
 failed_chains = {e["chain"] for e in errors if e["stage"] == "stores"}
 failed_stores = {(e["chain"], e["store"]) for e in errors if e["stage"] == "prices"}
 prev = Path("prev/sal.db")
 prev_count = 0
 if prev.exists():
  db.execute("ATTACH DATABASE ? AS prev", (str(prev),))
  try:
   prev_count = db.execute("SELECT COUNT(*) FROM prev.stores").fetchone()[0]
  except sqlite3.Error:
   prev_count = 0
  for r in copy_prev(db, today, failed_chains, failed_stores, have):
   r["he"] = chains[r["chain"]].name_he + f" (נתוני {r['stale']})" if r["chain"] in chains else r["chain"]
   rows.append(r)
   print("fallback:", r["chain"], r["store"], r["stale"])
  db.commit()
  db.execute("DETACH DATABASE prev")
 run_at = datetime.now(timezone.utc).isoformat()
 db.executemany("INSERT INTO meta VALUES(?,?)", [("run_at", run_at), ("data_date", today.isoformat())])
 db.commit()
 db.execute("VACUUM"); db.close()

 gz = OUT / "sal.db.gz"
 with open(db_path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
  shutil.copyfileobj(src, dst)
 raw, packed = round(db_path.stat().st_size / 1e6, 1), round(gz.stat().st_size / 1e6, 1)

 ok, why = publish_ok(fresh, len(rows), prev_count)
 if not ok and os.environ.get("FORCE_PUBLISH") == "1":
  ok, why = True, why + " (forced)"
 (OUT / "summary.json").write_text(json.dumps({"run_at": run_at, "data_date": today.isoformat(),
  "db_mb": raw, "gz_mb": packed, "fresh": fresh, "prev_stores": prev_count, "publish": ok, "guard": why,
  "stores": rows, "errors": errors}, ensure_ascii=False, indent=1), encoding="utf-8")
 s = os.environ.get("GITHUB_STEP_SUMMARY")
 if s:
  L = [f"## Stores: {len(rows)} ({fresh} fresh, prev run {prev_count}) | DB: {raw}MB | gz: {packed}MB", ""]
  if not ok:
   L += [f"### ⛔ NOT PUBLISHED: {why}", ""]
  L += ["| chain | store | city | items | promos |", "|---|---|---|---|---|"]
  c = lambda x: str(x).replace("|", "/")
  L += [f"| {c(r['he'])} | {c(r['store'])} | {c(r['city'])} | {r['items']} | {r['promos']} |" for r in rows]
  if errors:
   L += ["", f"### Errors ({len(errors)})", ""] + [f"- {e['chain']} {e.get('store','')}: {e['error']}" for e in errors]
  Path(s).write_text("\n".join(L), encoding="utf-8")
 if not ok:
  print("NOT PUBLISHING:", why)
 sys.stdout.flush(); sys.stderr.flush()
 # os._exit: do not wait for hung download threads at interpreter shutdown
 os._exit(0 if ok else 1)

if __name__ == "__main__":
 main()
