"""Export slim JSON files for the web app: out/sal.db -> site/data/"""
import json, re, sqlite3
from datetime import date
from pathlib import Path
import israeli_prices as ilp

DATA = Path("site/data"); (DATA / "s").mkdir(parents=True, exist_ok=True)

STOP = {"במשקל", "משקל", "ארוז", "לק\"ג", "ק\"ג", "קג", "לקג", "טרי", "טריים", "טריה", "מובחר", "מובחרים", "סוג", "א", "א'", "יח'", "יחידה", "יח", "כ", "לק", "ק", "ג", "גר", "גרם", "קילו", "מל", "ליטר", "יחי"}

def norm_name(n):
 n = re.sub(r"[\"'׳״()\[\].,*/\\\-]", " ", n or "")
 words = [w for w in n.split() if w not in STOP and not re.fullmatch(r"\d+", w)]
 if words:  # simple Hebrew plural -> singular on the head word: מלפפונים->מלפפון, עגבניות->עגבניה
  h = words[0]
  if len(h) > 4 and h.endswith("ים"): words[0] = h[:-2]
  elif len(h) > 4 and h.endswith("יות"): words[0] = h[:-3] + "יה"
  elif len(h) > 4 and h.endswith("ות"): words[0] = h[:-2] + "ה"
  w0 = words[0]
  if w0[-1] in "כמנפצ": words[0] = w0[:-1] + "ךםןףץ"["כמנפצ".index(w0[-1])]  # final letter form
 return " ".join(words[:4]).strip()

EGG_SKIP = ("אטריות", "נודלס", "שוקולד", "הפתעה", "עוגיות", "פסטה", "ביסקוויט", "חלבון", "מיונז", "סלט", "ביצת", "קינדר")

def egg_group(n):
 # Eggs come from many producers with different barcodes: group by size, count and type
 t = (n or "").replace("\u202b", "").replace("\u202c", "")
 if "ביצים" not in t or any(w in t for w in EGG_SKIP):
  return None
 m = re.search(r"(?<!\d)(6|10|12|18|20|24|30)(?!\d)", t)
 if not m:
  return None
 size = "XL" if ("XL" in t or "ענק" in t) else "L" if (re.search(r"(?<![A-Za-z])L(?![A-Za-z])", t) or "גדול" in t) \
  else "M" if (re.search(r"(?<![A-Za-z])M(?![A-Za-z])", t) or "בינוני" in t) else ""
 kind = next((k for k in ("אומגה", "אורגני", "חופש") if k in t), "")
 return " ".join(x for x in ["ביצים", size, m.group(1) + " יח׳", kind] if x)

def is_coupon(desc, mq, dr):
 d = desc or ""
 return ("קופון" in d or re.search(r"מעל\s*₪?\s*\d", d) is not None
   or (mq or 0) >= 50 or (dr or 0) >= 90)

def dump(path, obj):
 path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def main():
 he = {c.slug: c.name_he for c in ilp.list_chains()}
 db = sqlite3.connect("out/sal.db")
 today = date.today().isoformat()

 # 1) catalog + cross-chain groups (produce, deli, eggs)
 cat, groups, code2group = {}, {}, {}
 for c, n, m, q, u, w in db.execute("SELECT item_code, name, manufacturer, quantity, unit, is_weighted FROM products"):
  g = egg_group(n)
  if not g and (":" in c or len(c) < 8 or w):
   g = norm_name(n)  # produce / deli / weighted: chain-internal codes, group by normalized name
  if g:
   groups.setdefault(g, {"n": g, "u": "" if g.startswith("ביצים") else u, "w": 0 if g.startswith("ביצים") else int(bool(w)), "c": []})["c"].append(c)
   code2group[c] = g
   continue
  cat[c] = [n, m, q, u]
 for g, v in groups.items():
  cat["g:" + g] = [v["n"], "", None, v["u"], v["w"], v["c"]]

 # 2) price sanity inside groups: a price far from the group median is almost always a unit error
 #    (per unit vs per kg, per 100g) - drop it rather than show a wrong cheapest store
 gp = {}
 for c, p in db.execute("SELECT item_code, price FROM prices"):
  if c in code2group and p:
   gp.setdefault(code2group[c], []).append(p)
 med = {g: sorted(v)[len(v) // 2] for g, v in gp.items() if len(v) >= 3}
 dropped = 0

 # 3) per-store files
 links = {}
 for code, pid, sid in db.execute("SELECT pi.item_code, pi.pid, p.sid FROM promo_items pi JOIN promos p ON p.pid = pi.pid"):
  links.setdefault(sid, {}).setdefault(code, []).append(pid)
 stores = []
 cols = [r[1] for r in db.execute("PRAGMA table_info(stores)")]
 stale_col = "stale" if "stale" in cols else "0"
 for sid, chain, store_id, name, address, city, items, stale in db.execute(
   f"SELECT sid, chain, store_id, name, address, city, items, {stale_col} FROM stores ORDER BY chain, name").fetchall():
  key = f"{chain}-{store_id}"  # stable across nightly runs (sid is not)
  stores.append({"sid": key, "chain": chain, "he": he.get(chain, chain), "name": name,
   "address": address, "city": city, "items": items, "stale": int(stale or 0)})
  prices = {}
  for c, p, u in db.execute("SELECT item_code, price, unit_price FROM prices WHERE sid=?", (sid,)):
   g = code2group.get(c)
   if g in med and p and not (0.4 * med[g] <= p <= 3 * med[g]):
    dropped += 1
    continue
   prices[c] = [p, u]
  promos = {}
  for pid, desc, end, club, mq, dp, dr in db.execute(
    "SELECT pid, description, end, club, min_qty, discounted_price, discount_rate FROM promos WHERE sid=?", (sid,)):
   end = end or ""
   if end[:4].isdigit() and end[:10] < today:
    continue  # expired
   if is_coupon(desc, mq, dr):
    continue  # basket coupons / spend thresholds are not item prices
   promos[pid] = [desc, end[:10], club, mq, dp, dr]
  l = {c: [p for p in ps if p in promos] for c, ps in links.get(sid, {}).items() if c in prices}
  dump(DATA / "s" / f"{key}.json", {"p": prices, "pr": promos, "l": {c: ps for c, ps in l.items() if ps}})
 dump(DATA / "stores.json", {"updated": today, "stores": stores})
 dump(DATA / "catalog.json", cat)
 print(len(stores), "stores,", len(cat), "catalog entries,", len(groups), "groups,", dropped, "outlier prices dropped")

if __name__ == "__main__":
 main()
