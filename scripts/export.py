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
 cat, groups = {}, {}
 for c, n, m, q, u, w in db.execute("SELECT item_code, name, manufacturer, quantity, unit, is_weighted FROM products"):
  eg = egg_group(n)
  if eg:
   groups.setdefault(eg, {"n": eg, "u": "", "w": 0, "c": []})["c"].append(c)
   continue
  if ":" in c or len(c) < 8 or w:
   # produce / deli / weighted: chain-internal codes, so group by normalized name across chains
   g = norm_name(n)
   if g:
    groups.setdefault(g, {"n": g, "u": u, "w": int(bool(w)), "c": []})["c"].append(c)
    continue
  cat[c] = [n, m, q, u]
 for g, v in groups.items():
  cat["g:" + g] = [v["n"], "", None, v["u"], v["w"], v["c"]]
 dump(DATA / "catalog.json", cat)
 print(len(stores), "stores,", len(cat), "catalog entries,", len(groups), "groups")

if __name__ == "__main__":
 main()
