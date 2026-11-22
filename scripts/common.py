"""Pure helpers shared by fetch.py and export.py (no network, no israeli_prices) - unit tested in tests/."""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IL = ZoneInfo("Asia/Jerusalem")
MAX_STALE_DAYS = 3        # fallback data older than this is dropped, not shown as current
MIN_KEEP_RATIO = 0.7      # a run that keeps fewer stores than this share of the previous run is not published


def today_il():
    return datetime.now(IL).date()


def key(chain, code):
    """Real barcodes are shared across chains; short internal codes (produce, deli) are per chain."""
    code = str(code or "").strip()
    return code if code.isdigit() and len(code) >= 8 else f"{chain}:{code}"


# ---- promo normalization: one canonical form, so the site never has to guess ----
def norm_date(x):
    """datetime / date / str / None -> 'YYYY-MM-DD HH:MM:SS' or None (never the string 'None')."""
    if x is None:
        return None
    if isinstance(x, (datetime, date)):
        return x.isoformat(sep=" ") if isinstance(x, datetime) else x.isoformat()
    s = str(x).strip()
    return s if s[:4].isdigit() else None


def norm_rate(r):
    """Discount rate -> fraction 0..1. The transparency files use percent (25.0 = 25%), so >=1 is percent."""
    if r is None:
        return None
    try:
        r = float(r)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    return r / 100 if r >= 1 else r


def norm_club(c):
    """Club field comes as '0', '0 - כלל הלקוחות', '1=Yellow friends', '(3=פלטינום)', None... -> 0 (everyone) or the club number."""
    if c is None:
        return 0
    if isinstance(c, (int, float)):
        return int(c)
    s = str(c)
    if "כלל הלקוחות" in s:
        return 0
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


def norm_qty(q):
    """min_qty: weighted promos use 0.01 / 0.1 (kg); anything below 1 means 'no minimum'."""
    try:
        q = float(q)
    except (TypeError, ValueError):
        return 1.0
    return q if q >= 1 else 1.0


def is_coupon(desc, mq, rate):
    """Basket coupons / spend thresholds / gifts are not item prices. rate is a normalized fraction."""
    d = desc or ""
    return ("קופון" in d or re.search(r"מעל\s*₪?\s*\d", d) is not None
            or (mq or 0) >= 50 or ((rate or 0) >= 0.9 and (mq or 1) <= 1))


def promo_active(start, end, today):
    """today: 'YYYY-MM-DD'. Unknown dates count as active."""
    s, e = (start or "")[:10], (end or "")[:10]
    if s[:4].isdigit() and s > today:
        return False
    if e[:4].isdigit() and e < today:
        return False
    return True


# ---- fallback / publish guard ----
def fresh_enough(data_date, today, max_days=MAX_STALE_DAYS):
    if not data_date:
        return False
    try:
        d = date.fromisoformat(str(data_date)[:10])
    except ValueError:
        return False
    return (today - d) <= timedelta(days=max_days)


def publish_ok(fresh_count, total_count, prev_count, ratio=MIN_KEEP_RATIO):
    """Returns (ok, reason). Never replace yesterday's data with an empty or much smaller run."""
    if fresh_count == 0:
        return False, "no store was fetched today"
    if prev_count and total_count < ratio * prev_count:
        return False, f"only {total_count} stores vs {prev_count} in the previous run"
    return True, ""


# ---- product grouping (export) ----
STOP = {"במשקל", "משקל", "ארוז", "לק\"ג", "ק\"ג", "קג", "לקג", "טרי", "טריים", "טריה", "מובחר", "מובחרים", "סוג", "א", "א'",
        "יח'", "יחידה", "יח", "כ", "לק", "ק", "ג", "גר", "גרם", "קילו", "מל", "ליטר", "יחי"}
BIDI = re.compile(r"[‎‏‪-‮]")


def norm_name(n):
    n = BIDI.sub("", n or "")
    n = re.sub(r"[\"'׳״()\[\].,*/\\\-]", " ", n)
    words = [w for w in n.split() if w not in STOP and not re.fullmatch(r"[\d.]+", w)]
    if words:  # simple Hebrew plural -> singular on the head word: מלפפונים->מלפפון, עגבניות->עגבניה
        h = words[0]
        if len(h) > 4 and h.endswith("ים"):
            words[0] = h[:-2]
        elif len(h) > 4 and h.endswith("יות"):
            words[0] = h[:-3] + "יה"
        elif len(h) > 4 and h.endswith("ות"):
            words[0] = h[:-2] + "ה"
        w0 = words[0]
        if w0[-1] in "כמנפצ":
            words[0] = w0[:-1] + "ךםןףץ"["כמנפצ".index(w0[-1])]  # final letter form
    return " ".join(words[:4]).strip()


_UNITS = [(r"ק\"ג|קג|קילו", "ק\"ג"), (r"גרם|גר'?|ג'?", "גרם"), (r"מ\"ל|מל", "מ\"ל"), (r"ליטר|ל'?", "ליטר"),
          (r"יחידות|יח'?", "יח׳")]
SIZE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(" + "|".join(u for u, _ in _UNITS) + r")(?![א-ת])")


def size_tag(n):
    """'גזר ארוז 1 ק"ג' -> '1 ק"ג'; packs of different sizes must not be compared as one product."""
    m = SIZE_RE.search(BIDI.sub("", n or ""))
    if not m:
        return ""
    num = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
    unit = next(lbl for pat, lbl in _UNITS if re.fullmatch(pat, m.group(2)))
    return f"{num} {unit}"


EGG_SKIP = ("אטריות", "נודלס", "שוקולד", "הפתעה", "עוגיות", "פסטה", "ביסקוויט", "חלבון", "מיונז", "סלט", "ביצת", "קינדר")
EGG_COUNTS = {6, 10, 12, 18, 20, 24, 30}


def egg_group(n):
    """Eggs come from many producers with different barcodes: group by size, count and type."""
    t = BIDI.sub("", n or "")
    if "ביצים" not in t or any(w in t for w in EGG_SKIP):
        return None
    mm = re.search(r"(?<!\d)(\d+)\s*[xX×*]\s*(\d+)(?!\d)", t)  # "2X12" = 24 eggs
    if mm:
        count = int(mm.group(1)) * int(mm.group(2))
    else:
        m = re.search(r"(?<!\d)(6|10|12|18|20|24|30)(?!\d)", t)
        count = int(m.group(1)) if m else None
    if count not in EGG_COUNTS:
        return None
    size = "XL" if ("XL" in t or "ענק" in t) else "L" if (re.search(r"(?<![A-Za-z])L(?![A-Za-z])", t) or "גדול" in t) \
        else "M" if (re.search(r"(?<![A-Za-z])M(?![A-Za-z])", t) or "בינוני" in t) else ""
    kind = next((k for k in ("אומגה", "אורגני", "חופש") if k in t), "")
    return " ".join(x for x in ["ביצים", size, f"{count} יח׳", kind] if x)


def group_products(rows):
    """rows: iterable of (code, name, weighted). Returns {code: group_name} for codes that belong to a cross-chain group.
    Weighted (price per kg) and per-unit items with the same name are split, so a kg price is never compared to a unit price."""
    pre = {}
    for code, name, w in rows:
        g = egg_group(name)
        if g:
            pre[code] = (g, None)  # eggs: always per pack
            continue
        if ":" in code or len(code) < 8 or w:
            base = norm_name(name)
            if not base:
                continue
            tag = size_tag(name)
            pre[code] = (f"{base} {tag}" if tag else base, int(bool(w)))
    kinds = {}
    for g, w in pre.values():
        if w is not None:
            kinds.setdefault(g, set()).add(w)
    out = {}
    for code, (g, w) in pre.items():
        if w == 0 and len(kinds.get(g, ())) > 1:
            g += " ליחידה"
        out[code] = g
    return out


# ---- substitutes: "the same product in principle, similar size" (export -> site/data/subs.json) ----
# Precision over recall: when in doubt there is no substitute. An unknown brand stays in the name, so the
# head word does not match and no substitute is offered rather than a wrong one.
# single, unambiguous brand words only (never a word that also names a product type: פרי, עוף, פסטה, נקטר...)
BRANDS = set("""
אסם היינץ היינז עלית תנובה שטראוס יטבתה טרה סוגת ויסוצקי יכין זוגלובק שופרסל יוחננוף ויקטורי קרפור ברקת אחלה
צבר אנגל ברמן דורות פרימור פריגת נביעות נסטלה נסקפה גייקובס ג'ייקובס לנדוור קליק מיה ניוואה קולגייט דאב פנטן
גונסון ג'ונסון האגיס הוגיס פמפרס טיטול סנו פיירי ונוס פרסיל אריאל מטרנה סימילאק מוטי ברילה ברילא גלעם סטארקיסט
סטרקיסט כרמית לוטוס מילקה נוטלה הרשי קינדר פררו מקסוול קוקה פפסי ספרייט פאנטה שוופס ספרינג טבעול תלמה קנור
דנונה מולר יופלה אקטיביה טירת זוגלובק תפוגן מאסטר מעדנות רמילוי
""".split())
# a flavor word is part of the product's identity: plain ketchup is not barbecue ketchup, plain coffee is not vanilla
FLAVOR_WORDS = set("""
ברביקיו גריל שום צילי צ'ילי סרירצה סרירצ'ה פפריקה מעושן מעושנת חרדל דבש וניל מוקה קרמל שוקולד מריר לבן תות בננה
לימון תפוז תפוח אפרסק מנגו ענבים אננס דובדבן פטל אוכמניות מנטה נענע קינמון אגוזים בוטנים לוז שקדים פיסטוק קוקוס
בצל עגבניות זיתים פטריות כמהין גבינה שמנת עוף בקר טונה סלמון שישליצני פסטו בזיליקום אורגנו ים טבעי מלח מלוח אגוז
""".split())
PACK_WORDS = {"מארז", "שלישייה", "שלישיית", "רביעייה", "רביעיית", "זוג", "זוגות", "שישייה", "שישיית", "חמישייה", "מאגדת", "מגש"}
STOP_WORDS = {"בקבוק", "לחיץ", "פחית", "שקית", "בשקית", "קופסה", "קופסא", "בקופסא", "בקופסה", "צנצנת", "בצנצנת", "בקרטון",
              "קרטון", "חדש", "חדשה", "מבצע", "יחידה", "יחידות", "יח", "גרם", "גר", "מל", "ליטר", "קג", "כשר", "מהדרין", "בדץ",
              "כשל", "כשלפ", "פרווה", "ארוז", "גדול", "גדולה", "קטן", "קטנה", "של", "את", "ג", "ל", "ק"}
# what a "ללא / מופחת / דל" removes; only these words are swallowed, the rest of the name still counts
LOW_TARGETS = {"סוכר", "מלח", "תוספת", "גלוטן", "לקטוז", "שומן", "נתרן", "קלוריות", "כולסטרול", "שמן", "חומרים",
               "משמרים", "סוכרים", "מתוק", "אלכוהול", "קפאין"}
LOW = {"מופחת", "מופחתת", "פחות", "דל", "דלת", "ללא", "ללת", "לייט", "דיאט", "זירו", "קל"}
VARIANT = {"חריף": "spicy", "חריפה": "spicy", "פיקנטי": "spicy", "אורגני": "organic", "אורגנית": "organic",
           "טבעוני": "vegan", "טבעונית": "vegan", "ללג": "gluten", "גלוטן": "gluten",
           "קפוא": "frozen", "קפואה": "frozen", "קפואים": "frozen", "קפואות": "frozen",
           "סמול": "size:s", "מדיום": "size:m", "לארג": "size:l", "לארג'": "size:l", "אקסטרה": "x"}
# numbers that define the product, not its pack size: diaper size 4 is not size 5, pasta no. 8 is not no. 3
_IDENT = re.compile(r"(?<![א-ת])(מידה|מס|מספר|שלב|גוון)\s*['׳.]?\s*\+?\s*(\d+)")
# words that name what the product IS: if one name has one of these and the other does not, they differ
# (shampoo vs conditioner from the same line share almost every other word)
TYPE_WORDS = set("""
שמפו מרכך סבון ג'ל גל קרם תחליב תרסיס ספריי מסכה סרום אבקת אבקה נוזל טבליות קפסולות כמוסות משחת דאודורנט מי
מגבונים ממרח רוטב משקה מיץ נקטר תרכיז סירופ חטיף חטיפי עוגיות עוגה עוגת מרק תערובת קמח פתיתים אטריות
מכנסונים פנטס סטיק רול מברשת
""".split())
# one name, two products (dry chickpeas / hummus salad, raw tahini / tahini salad, canned / dry): never substitute
AMBIGUOUS = {"חומוס", "טחינה", "תירס", "שעועית", "אפונה", "חציל", "פלפל", "עדשים"}
FLAVOR = {"בטעם", "בסגנון", "עם", "במילוי", "בניחוח", "ניחוח"}
# grams and millilitres are kept apart: laundry gel (litres) is not powder (kilograms)
_UNIT_MUL = {"ק\"ג": ("g", 1000), "גרם": ("g", 1), "מ\"ל": ("ml", 1), "ליטר": ("ml", 1000), "יח׳": ("u", 1)}
_CARTON = re.compile(r"\(\s*\d+\s*\)")           # "(12)" = carton count printed in the name
_MULT = re.compile(r"(?<![\d.])(\d+)\s*[*xX×]\s*(\d+(?:\.\d+)?)")


def _tokens(name):
    n = BIDI.sub("", name or "")
    n = _CARTON.sub(" ", n)
    n = re.sub(r"(\d)([א-ת])", r"\1 \2", n)
    n = re.sub(r"([א-ת])(\d)", r"\1 \2", n)
    return n


def product_features(name):
    """-> dict(head, core, pct, var, pack, size, cls) or None when the product cannot be matched safely."""
    raw = _tokens(name)
    pack = False
    m = _MULT.search(raw)
    if m and 2 <= int(m.group(1)) <= 8:
        pack = True                       # "2*140 גר", "6*24 ג" = a multipack; "9*750" = carton count, ignored
    tag = size_tag(raw.replace("*", " "))
    size = cls = None
    if tag:
        num, unit = tag.split(" ", 1)
        cls, mul = _UNIT_MUL[unit]
        size = float(num) * mul
    ident = [f"{a}{b}" for a, b in _IDENT.findall(raw)]
    # a bare number that is not the pack size, a %, or "N*M" is a model / shape number ("פסטה רגיה 19")
    rest = _IDENT.sub(" ", SIZE_RE.sub(" ", _MULT.sub(" ", raw)))
    rest = re.sub(r"\d+(?:\.\d+)?\s*%", " ", rest)
    ident += [f"n{int(x)}" for x in re.findall(r"(?<![\d.])(\d{1,4})(?![\d.])", rest)]
    pct = tuple(sorted(re.findall(r"(\d+(?:\.\d+)?)\s*%", raw)) + sorted(ident))
    words = re.sub(r"[\"'׳״()\[\].,*/\\\-+:!%]", " ", raw).split()
    core, var, mode = [], set(), None
    for w in words:
        if not re.fullmatch(r"[א-ת]{2,}", w):
            continue                      # numbers, units, latin brand names
        if w in PACK_WORDS:
            pack = True; continue
        if w in LOW:
            var.add("low"); mode = "low"; continue
        if w in VARIANT:
            var.add(VARIANT[w]); continue
        if w in FLAVOR:
            mode = "flavor"; continue
        if mode == "low":                 # "ללא תוספת סוכר ומלח": the removed things describe the variant
            b = w[1:] if w.startswith("ו") and w[1:] in LOW_TARGETS else w
            if b in LOW_TARGETS:
                var.add("low:" + b); continue
            mode = None
        if mode == "flavor":              # every word after "עם / בטעם / במילוי": "עם קציפת תות" != "עם קציפת לימון"
            if w not in STOP_WORDS and w not in BRANDS:
                var.add("flavor:" + w)
            continue
        if w in STOP_WORDS or w in BRANDS:
            continue
        if core and w in FLAVOR_WORDS:   # not the head word: "שום כתוש" is garlic, "קטשופ שום" is a flavor
            var.add("flavor:" + w); continue
        core.append(w)
    if re.search(r"(?i)\b(zero|light|lite|diet)\b", raw):
        var.add("low")
    if not core:
        return None
    if len(core) == 1 and core[0] in FLAVOR_WORDS:
        return None                       # "פאנטה ענבים" / "נביעות + ענבים": only a flavor is left, the kind is unknown
    return {"head": core[0], "core": core, "pct": pct, "var": tuple(sorted(var)), "pack": pack, "size": size, "cls": cls}


def similarity(a, b):
    sa, sb = set(a["core"]), set(b["core"])
    return len(sa & sb) / len(sa | sb)


def substitutes(products, top=6, size_tol=0.2, min_sim=0.66, unit_price=None, max_ratio=1.8):
    """products: {code: name}; unit_price: optional {code: typical price per g/ml/unit}. Returns ({code: [[sub_code, score], ...]}, {code: [size, cls]}).
    A substitute has the same head word, the same % values (3% milk is not 1%), the same variant set
    (regular vs spicy / reduced sugar / organic / flavored), neither is a multipack, the same unit class and a
    size within size_tol, and a name similarity of at least min_sim. When unit prices are known, the price per
    g/ml may differ by at most max_ratio: the same name can hide two products ("חומוס" = dry chickpeas or hummus)."""
    unit_price = unit_price or {}
    feats = {c: f for c, n in products.items() if (f := product_features(n)) and f["size"] and not f["pack"]}
    feats = {c: f for c, f in feats.items() if f["head"] not in AMBIGUOUS}
    groups = {}
    for c, f in feats.items():
        groups.setdefault((f["head"], f["pct"], f["var"], f["cls"]), []).append(c)
    out = {}
    for codes in groups.values():
        if len(codes) < 2:
            continue
        for c in codes:
            a, cand = feats[c], []
            for d in codes:
                if d == c:
                    continue
                b = feats[d]
                if abs(a["size"] - b["size"]) > size_tol * a["size"]:
                    continue
                pa, pb = unit_price.get(c), unit_price.get(d)
                if pa and pb and max(pa, pb) > max_ratio * min(pa, pb):
                    continue
                if set(a["core"]) & TYPE_WORDS != set(b["core"]) & TYPE_WORDS:
                    continue
                s = similarity(a, b)
                if s >= min_sim:
                    cand.append((s, -abs(a["size"] - b["size"]), d))
            if cand:
                cand.sort(reverse=True)
                out[c] = [[d, round(s, 2)] for s, _, d in cand[:top]]
    used = set(out) | {d for v in out.values() for d, _ in v}
    sizes = {c: [feats[c]["size"], feats[c]["cls"]] for c in used}
    return out, sizes
