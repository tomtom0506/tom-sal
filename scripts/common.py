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
