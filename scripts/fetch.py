"""Nightly fetch: prices + promos for stores in the user's area -> out/sal.db.gz

Two sources run side by side:
  over     - over.org.il API (mirror of the chains' official files) - primary
  official - the chains' own portals via israeli_prices            - fallback + nightly cross-check
Merge order per store: over -> official -> previous night (max MAX_STALE_DAYS old).
out/compare.json holds the nightly price comparison between the two sources."""
import gzip, json, os, re, shutil, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
import israeli_prices as ilp
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import key, norm_date, norm_rate, norm_club, norm_qty, today_il, fresh_enough, publish_ok
import over

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
  stale INT DEFAULT 0, data_date TEXT, source TEXT, chain_he TEXT);
 CREATE TABLE products(item_code TEXT PRIMARY KEY, name, manufacturer, quantity REAL,
  unit, is_weighted INT) WITHOUT ROWID;
 CREATE TABLE prices(item_code TEXT, sid INT, price REAL, unit_price REAL,
  PRIMARY KEY(item_code, sid)) WITHOUT ROWID;
 CREATE TABLE promos(pid INTEGER PRIMARY KEY, sid INT, promotion_id, description,
  start TEXT, end TEXT, club INT, min_qty REAL, discounted_price REAL, discount_rate REAL);
 CREATE TABLE promo_items(item_code TEXT, pid INT, PRIMARY KEY(item_code, pid)) WITHOUT ROWID;
"""
# lookups by store / promo while merging; dropped before publishing to keep the file small
INDEXES = ["prices(sid)", "promos(sid)", "promo_items(pid)"]


def add_indexes(db, alias="main"):
 for i, spec in enumerate(INDEXES):
  db.execute(f"CREATE INDEX IF NOT EXISTS {alias}.ix_{i} ON {spec}")


def new_db(path):
 path = Path(path)
 if path.exists(): path.unlink()
 db = sqlite3.connect(path)
 db.executescript(SCHEMA)
 return db


def insert_store(db, chain, st, items, promos, today, chain_he=None):
 """Official source (israeli_prices objects)."""
 sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos,data_date,source,chain_he) VALUES(?,?,?,?,?,?,?,?,?,?)",
   (chain, str(st.store_id), st.name, st.address, st.city, len(items), len(promos), today, "official", chain_he)).lastrowid
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
 return sid


def insert_over(db, slug, store_id, data, chain_he=None):
 """over.org.il source (dicts of strings from over.fetch_store)."""
 m, items = data.get("meta") or {}, data["items"]
 sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos,data_date,source,chain_he) VALUES(?,?,?,?,?,?,?,?,?,?)",
   (slug, str(store_id), m.get("store_name"), m.get("address"), m.get("city"), len(items), 0, data.get("as_of"), "over", chain_he)).lastrowid
 w = lambda x: int(str(x or "").strip().lower() in ("1", "true", "yes"))
 db.executemany("INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?)", [
  (key(slug, r["item_code"]), r.get("item_name"), r.get("manufacturer_name"), over.num(r.get("quantity")),
   r.get("unit_of_measure"), w(r.get("is_weighted"))) for r in items])
 db.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?)", [
  (key(slug, r["item_code"]), sid, over.num(r.get("item_price")), over.num(r.get("unit_of_measure_price")))
  for r in items if over.num(r.get("item_price")) is not None])
 n = 0
 for p, (dp, dr, mq), codes in over.split_promos(data["promos"], data["pitems"]):
  pid = db.execute("INSERT INTO promos(sid,promotion_id,description,start,end,club,min_qty,discounted_price,discount_rate) VALUES(?,?,?,?,?,?,?,?,?)",
    (sid, p["promotion_id"], p.get("description"), norm_date(p.get("start_date")), norm_date(p.get("end_date")),
     norm_club(p.get("club_id")), norm_qty(mq), dp, norm_rate(dr))).lastrowid
  db.executemany("INSERT OR IGNORE INTO promo_items VALUES(?,?)", [(key(slug, c), pid) for c in codes])
  n += 1
 db.execute("UPDATE stores SET promos=? WHERE sid=?", (n, sid))
 return sid


def table_cols(db, alias, table):
 return [r[1] for r in db.execute(f"PRAGMA {alias}.table_info({table})")]


def copy_store(db, alias, osid, store_id=None, stale=0, ddate=None, source=None):
 """Copy one store with its prices/products/promos from an attached DB into main. Promo fields are re-normalized
 (previous-night DBs may predate normalization)."""
 cols = table_cols(db, alias, "stores")
 he = "chain_he" if "chain_he" in cols else "NULL"
 chain, sid0, name, address, city, items, promos, chain_he = db.execute(
  f"SELECT chain, store_id, name, address, city, items, promos, {he} FROM {alias}.stores WHERE sid=?", (osid,)).fetchone()
 sid = db.execute("INSERT INTO stores(chain,store_id,name,address,city,items,promos,stale,data_date,source,chain_he) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
   (chain, str(store_id or sid0), name, address, city, items, promos, int(stale), ddate, source, chain_he)).lastrowid
 db.execute(f"INSERT OR IGNORE INTO products SELECT pr.* FROM {alias}.products pr JOIN {alias}.prices p ON p.item_code=pr.item_code WHERE p.sid=?", (osid,))
 db.execute(f"INSERT OR REPLACE INTO prices SELECT item_code, ?, price, unit_price FROM {alias}.prices WHERE sid=?", (sid, osid))
 for opid, pr_id, desc, start, end, club, mq, dp, dr in db.execute(
   f"SELECT pid, promotion_id, description, start, end, club, min_qty, discounted_price, discount_rate FROM {alias}.promos WHERE sid=?", (osid,)).fetchall():
  pid = db.execute("INSERT INTO promos(sid,promotion_id,description,start,end,club,min_qty,discounted_price,discount_rate) VALUES(?,?,?,?,?,?,?,?,?)",
    (sid, pr_id, desc, norm_date(start), norm_date(end), norm_club(club), norm_qty(mq), dp, norm_rate(dr))).lastrowid
  db.execute(f"INSERT OR IGNORE INTO promo_items SELECT item_code, ? FROM {alias}.promo_items WHERE pid=?", (pid, opid))
 return sid


def stores_in(db, alias):
 """{(chain, bare store_id): (sid, store_id, data_date)} of an attached DB (empty if the DB has no stores table)."""
 cols = table_cols(db, alias, "stores")
 if not cols:
  return {}
 dc = "data_date" if "data_date" in cols else "NULL"
 return {(c, over.bare(st)): (sid, str(st), dd) for sid, c, st, dd in db.execute(f"SELECT sid, chain, store_id, {dc} FROM {alias}.stores")}


def merge(db, today, aliases):
 """Build the published DB. aliases: attached DB names among 'ov' (over), 'off' (official), 'prev'.
 Per store: over -> official -> previous night if at most MAX_STALE_DAYS old. Store ids keep the official
 spelling (e.g. '045') so the site's saved selections stay valid. Returns summary rows."""
 T = today.isoformat()
 for a in aliases:
  try:
   add_indexes(db, a)
  except sqlite3.Error:
   pass  # e.g. an empty or read-only source
 S = {a: stores_in(db, a) for a in ("ov", "off", "prev") if a in aliases}
 canon = {}
 for a in ("prev", "off"):  # official spelling wins
  for k, (_, st, _) in S.get(a, {}).items():
   canon[k] = st
 rows = []
 for k in sorted(set().union(*[set(v) for v in S.values()]) if S else set()):
  # the fresher of the two sources; on the same date over.org.il wins
  cands = []
  for rank, (a, src) in enumerate((("ov", "over"), ("off", "official"))):
   if k in S.get(a, {}):
    osid, st, dd = S[a][k]
    dd = (dd or T)[:10]
    if fresh_enough(dd, today):
     cands.append((dd, -rank, (a, osid, dd, src)))
  pick = max(cands)[2] if cands else None
  if not pick and k in S.get("prev", {}):
   osid, st, dd = S["prev"][k]
   # DBs from before v1.6.0 carry no date: they are at most one night old
   dd = (dd or today.fromordinal(today.toordinal() - 1).isoformat())[:10]
   if fresh_enough(dd, today):
    pick = ("prev", osid, dd, "prev")
   else:
    print("too old, dropped:", k, dd)
  if not pick:
   continue
  a, osid, dd, src = pick
  copy_store(db, a, osid, canon.get(k), stale=dd < T, ddate=dd, source=src)
  name, items, promos = db.execute(f"SELECT name, items, promos FROM {a}.stores WHERE sid=?", (osid,)).fetchone()
  rows.append({"chain": k[0], "store": name, "items": items, "promos": promos, "source": src, "date": dd})
 return rows


def compare(db):
 """Price-by-price comparison of stores present in both sources (attached as 'ov' and 'off')."""
 ov, off = stores_in(db, "ov"), stores_in(db, "off")
 per, tot = [], {"stores": 0, "both": 0, "equal": 0, "only_official": 0, "only_over": 0}
 for k in sorted(set(ov) & set(off)):
  a, b = off[k][0], ov[k][0]
  both, equal = db.execute("SELECT COUNT(*), COALESCE(SUM(ABS(o.price - v.price) < 0.005), 0) FROM off.prices o "
    "JOIN ov.prices v ON v.item_code=o.item_code AND v.sid=? WHERE o.sid=?", (b, a)).fetchone()
  only_off = db.execute("SELECT COUNT(*) FROM off.prices o WHERE o.sid=? AND NOT EXISTS "
    "(SELECT 1 FROM ov.prices v WHERE v.sid=? AND v.item_code=o.item_code)", (a, b)).fetchone()[0]
  only_ov = db.execute("SELECT COUNT(*) FROM ov.prices v WHERE v.sid=? AND NOT EXISTS "
    "(SELECT 1 FROM off.prices o WHERE o.sid=? AND o.item_code=v.item_code)", (b, a)).fetchone()[0]
  diffs = db.execute("SELECT o.item_code, COALESCE(p.name, ''), o.price, v.price FROM off.prices o "
    "JOIN ov.prices v ON v.item_code=o.item_code AND v.sid=? LEFT JOIN off.products p ON p.item_code=o.item_code "
    "WHERE o.sid=? AND ABS(o.price - v.price) >= 0.005 ORDER BY ABS(o.price - v.price) DESC LIMIT 5", (b, a)).fetchall()
  dates = (off[k][2], ov[k][2])
  per.append({"chain": k[0], "store_id": k[1], "both": both, "equal": equal, "only_official": only_off, "only_over": only_ov,
   "official_date": dates[0], "over_date": dates[1],
   "diffs": [{"code": c, "name": n, "official": po, "over": pv} for c, n, po, pv in diffs]})
  for f in ("both", "equal", "only_official", "only_over"):
   tot[f] += per[-1][f]
  tot["stores"] += 1
 tot["equal_pct"] = round(100 * tot["equal"] / tot["both"], 2) if tot["both"] else None
 tot["only_official_stores"] = sorted(f"{c}-{s}" for c, s in set(off) - set(ov))
 tot["only_over_stores"] = sorted(f"{c}-{s}" for c, s in set(ov) - set(off))
 return {"total": tot, "stores": per}


def main():
 today = today_il(); T = today.isoformat()
 off = new_db(OUT / "official.db"); ovdb = new_db(OUT / "over.db")
 chains = {c.slug: c for c in ilp.list_chains() if c.implemented}
 he = {c.slug: c.name_he for c in ilp.list_chains()}
 jobs, errors = [], []
 for slug in chains:
  try:
   for st in get_stores(slug).stores:
    if in_area(st): jobs.append((slug, st))
  except Exception as e:
   errors.append({"chain": slug, "source": "official", "stage": "stores", "error": f"{type(e).__name__}: {str(e)[:120]}"})
 print(f"official: {len(jobs)} stores in area")

 prev = Path("prev/sal.db")
 prev_keys, prev_count = set(), 0
 if prev.exists():
  try:
   p = sqlite3.connect(prev)
   prev_keys = {(c, over.bare(s)) for c, s in p.execute("SELECT chain, store_id FROM stores")}
   prev_count = len(prev_keys)
   p.close()
  except sqlite3.Error:
   pass

 # over.org.il: every store it places in the area, plus every store the official portals or last night know about
 client, over_jobs, over_he = over.Client(), {}, {}
 try:
  over_he = over.chain_names(client)
  for r in over.area_stores(client, AREA_CODES):
   over_jobs[(over.slug_of(r["chain"]), over.bare(r["store_id"]))] = (r["chain"], r["store_id"])
  for k in {(c, over.bare(st.store_id)) for c, st in jobs} | prev_keys:
   if k not in over_jobs and k[0] in over.KEY:
    over_jobs[k] = (over.KEY[k[0]], k[1])
 except Exception as e:  # API down: skip it tonight, official + previous night cover
  errors.append({"chain": "*", "source": "over", "stage": "stores", "error": f"{type(e).__name__}: {str(e)[:120]}"})
  over_jobs = {}
 print(f"over: {len(over_jobs)} stores to fetch")

 # No `with`: leaving a with-block waits for stuck threads, so one hung store would kill the whole night.
 p_off, p_ov = ThreadPoolExecutor(max_workers=6), ThreadPoolExecutor(max_workers=4)
 f_off = {p_off.submit(fetch_store, c, s): (c, s) for c, s in jobs}
 f_ov = {p_ov.submit(over.fetch_store, client, ck, sid): k for k, (ck, sid) in over_jobs.items()}
 done, not_done = wait(list(f_off) + list(f_ov), timeout=RUN_BUDGET)
 p_off.shutdown(wait=False, cancel_futures=True); p_ov.shutdown(wait=False, cancel_futures=True)

 for f, (c, s) in f_off.items():
  try:
   if f not in done: raise TimeoutError(f"timeout after {RUN_BUDGET}s")
   chain, st, items, promos = f.result()
  except Exception as e:
   errors.append({"chain": c, "store": str(s.store_id), "source": "official", "stage": "prices", "error": f"{type(e).__name__}: {str(e)[:120]}"})
   continue
  insert_store(off, chain, st, items, promos, T, he.get(chain))
 off.commit()
 for f, k in f_ov.items():
  try:
   if f not in done: raise TimeoutError(f"timeout after {RUN_BUDGET}s")
   data = f.result()
  except Exception as e:
   errors.append({"chain": k[0], "store": k[1], "source": "over", "stage": "prices", "error": f"{type(e).__name__}: {str(e)[:120]}"})
   continue
  if data:
   insert_over(ovdb, k[0], over_jobs[k][1], data, he.get(k[0]) or over_he.get(over_jobs[k][0]))
 ovdb.commit()
 n_off = off.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
 n_ov = ovdb.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
 print(f"fetched: over {n_ov} stores ({client.requests} requests, {client.bytes / 1e6:.1f}MB), official {n_off} stores")
 off.close(); ovdb.close()

 db = new_db(OUT / "sal.db")
 db.execute("ATTACH DATABASE ? AS off", (str(OUT / "official.db"),))
 db.execute("ATTACH DATABASE ? AS ov", (str(OUT / "over.db"),))
 aliases = {"off", "ov"}
 if prev.exists():
  db.execute("ATTACH DATABASE ? AS prev", (str(prev),)); aliases.add("prev")
 rows = merge(db, today, aliases)
 for r in rows:
  r["he"] = he.get(r["chain"]) or over_he.get(over.KEY.get(r["chain"], ""), r["chain"])
 cmp = compare(db)
 db.commit()
 for a in aliases:
  db.execute(f"DETACH DATABASE {a}")
 for i in range(len(INDEXES)):
  db.execute(f"DROP INDEX IF EXISTS ix_{i}")
 run_at = datetime.now(timezone.utc).isoformat()
 db.executemany("INSERT INTO meta VALUES(?,?)", [("run_at", run_at), ("data_date", T)])
 db.commit()
 db.execute("VACUUM"); db.close()

 gz = OUT / "sal.db.gz"
 with open(OUT / "sal.db", "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
  shutil.copyfileobj(src, dst)
 raw, packed = round((OUT / "sal.db").stat().st_size / 1e6, 1), round(gz.stat().st_size / 1e6, 1)

 fresh = sum(1 for r in rows if r["date"] == T and r["source"] != "prev")
 ok, why = publish_ok(fresh, len(rows), prev_count)
 if not ok and os.environ.get("FORCE_PUBLISH") == "1":
  ok, why = True, why + " (forced)"
 by_src = {s: sum(r["source"] == s for r in rows) for s in ("over", "official", "prev")}
 (OUT / "compare.json").write_text(json.dumps({"date": T, **cmp}, ensure_ascii=False, indent=1), encoding="utf-8")
 hist_p = Path("prev/compare-history.json")
 try:
  hist = json.loads(hist_p.read_text(encoding="utf-8")) if hist_p.exists() else []
 except ValueError:
  hist = []
 hist = [h for h in hist if h.get("date") != T] + [{"date": T, **cmp["total"]}]
 (OUT / "compare-history.json").write_text(json.dumps(hist[-120:], ensure_ascii=False, indent=1), encoding="utf-8")
 (OUT / "summary.json").write_text(json.dumps({"run_at": run_at, "data_date": T,
  "db_mb": raw, "gz_mb": packed, "fresh": fresh, "prev_stores": prev_count, "by_source": by_src, "publish": ok, "guard": why,
  "over_requests": client.requests, "over_mb": round(client.bytes / 1e6, 1),
  "stores": rows, "errors": errors}, ensure_ascii=False, indent=1), encoding="utf-8")

 s = os.environ.get("GITHUB_STEP_SUMMARY")
 if s:
  t = cmp["total"]
  L = [f"## Stores: {len(rows)} ({fresh} fresh · over {by_src['over']} · official {by_src['official']} · prev {by_src['prev']}) | DB {raw}MB | gz {packed}MB", ""]
  if not ok:
   L += [f"### ⛔ NOT PUBLISHED: {why}", ""]
  L += [f"### over.org.il vs official: {t['equal_pct']}% equal prices ({t['equal']}/{t['both']} items in {t['stores']} stores)",
   f"only in official: {t['only_official']} items · only in over: {t['only_over']} items", ""]
  L += ["| chain | store | items | equal | only official | only over | top diff |", "|---|---|---|---|---|---|---|"]
  c = lambda x: str(x).replace("|", "/")
  for r in cmp["stores"]:
   dtop = r["diffs"][0] if r["diffs"] else None
   L.append(f"| {r['chain']} | {r['store_id']} | {r['both']} | {r['equal']} | {r['only_official']} | {r['only_over']} | "
    + (c(f"{dtop['name'][:30]}: {dtop['official']} vs {dtop['over']}") if dtop else "") + " |")
  L += ["", "| chain | store | source | date | items | promos |", "|---|---|---|---|---|---|"]
  L += [f"| {c(r['he'])} | {c(r['store'])} | {r['source']} | {r['date']} | {r['items']} | {r['promos']} |" for r in rows]
  if errors:
   L += ["", f"### Errors ({len(errors)})", ""] + [f"- [{e.get('source','')}] {e['chain']} {e.get('store','')}: {e['error']}" for e in errors]
  Path(s).write_text("\n".join(L), encoding="utf-8")
 if not ok:
  print("NOT PUBLISHING:", why)
 sys.stdout.flush(); sys.stderr.flush()
 # os._exit: do not wait for hung download threads at interpreter shutdown
 os._exit(0 if ok else 1)

if __name__ == "__main__":
 main()
