"""בדיקת נגישות: אילו רשתות אפשר למשוך מהן נתונים מתוך GitHub Actions.
לכל רשת: מושך את קובץ הסניפים, שומר אותו ב-data/stores/<slug>.json
ומסכם ב-data/probe.json ובטבלה בסיכום הריצה."""
import json, os, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone
from pathlib import Path
import israeli_prices as ilp

TIMEOUT = 90  # שניות לכל רשת
OUT = Path("data"); (OUT / "stores").mkdir(parents=True, exist_ok=True)

def probe(chain):
    t0 = time.time()
    sf = ilp.get_stores(chain.slug)
    stores = [s.model_dump() for s in sf.stores]
    (OUT / "stores" / f"{chain.slug}.json").write_text(
        json.dumps({"chain": chain.slug, "name_he": chain.name_he,
                    "last_updated": str(sf.last_updated), "stores": stores},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return len(stores), round(time.time() - t0, 1)

def main():
    chains = [c for c in ilp.list_chains() if c.implemented]
    results = []
    pool = ThreadPoolExecutor(max_workers=6)
    futs = {c.slug: (c, pool.submit(probe, c)) for c in chains}
    for slug, (c, f) in futs.items():
        r = {"chain": slug, "name_he": c.name_he, "family": c.portal_family, "sector": c.sector}
        try:
            n, sec = f.result(timeout=TIMEOUT)
            r.update(ok=True, stores=n, seconds=sec)
        except FTimeout:
            r.update(ok=False, error="timeout")
        except Exception as e:
            r.update(ok=False, error=f"{type(e).__name__}: {str(e)[:150]}")
        results.append(r)
        print(("OK  " if r["ok"] else "FAIL"), slug, r.get("stores", r.get("error")))
    pool.shutdown(wait=False, cancel_futures=True)

    ok = sum(r["ok"] for r in results)
    (OUT / "probe.json").write_text(json.dumps(
        {"run_at": datetime.now(timezone.utc).isoformat(), "ok": ok,
         "total": len(results), "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"## בדיקת נגישות: {ok}/{len(results)} רשתות זמינות", "",
                 "| רשת | פורטל | סטטוס | סניפים / שגיאה |", "|---|---|---|---|"]
        for r in sorted(results, key=lambda x: not x["ok"]):
            lines.append(f"| {r['name_he']} | {r['family']} | {'✅' if r['ok'] else '❌'} | "
                         f"{r.get('stores', r.get('error', ''))} |")
        Path(summary).write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
