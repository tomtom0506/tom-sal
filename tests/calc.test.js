// Run: node --test tests/
// Extracts the pure price logic between the ==calc-start== / ==calc-end== markers in index.html.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const src = html.split("// ==calc-start==")[1].split("\n").slice(1).join("\n").split("// ==calc-end==")[0];
const C = new Function(src + "; return C;")();
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, `${a} != ${b}`);
const T = "2026-11-20";

test("X for Y with a remainder: extra units at the regular price", () => {
 // bug: 3 units of "2 ב-20" (regular 15) showed 30 instead of 35
 const pr = ["2 ב-20", "", 0, 2, 20, null, ""];
 near(C.promoLine(15, pr, 2), 20);
 near(C.promoLine(15, pr, 3), 35);
 near(C.promoLine(15, pr, 4), 40);
 assert.strictEqual(C.promoLine(15, pr, 1), null);
 // "3 ב-10", regular 4, 4 units -> 10 + 4 = 14 (was 13.33)
 near(C.promoLine(4, ["3 ב-10", "", 0, 3, 10, null, ""], 4), 14);
});

test("real file rows: bundle vs per-unit price", () => {
 // dor-alon: "2 ב 20", rate 28.06% is the per-unit discount -> bundle
 near(C.promoLine(13.9, ["מבצע שעועית קינג קול 2 ב 20 שח", "", 0, 2, 20, 0.2806, ""], 2), 20);
 // wolt: dp 12.5 is per unit (rate 16.11% = 1 - 12.5/14.9)
 near(C.promoLine(14.9, ["12.50 מגבונים", "", 0, 2, 12.5, 0.1611, ""], 2), 25);
 // shufersal "2ב20", no rate: dp > price -> bundle
 near(C.promoLine(11.9, ["2ב20 שעועית", "", 0, 2, 20, null, ""], 2), 20);
 // simple unit price promo
 near(C.promoLine(10, ["ב-8", "", 0, 1, 8, null, ""], 3), 24);
});

test("second at N% applies to one unit per group", () => {
 // bug: "השני ב-50%" was applied to every unit (10 instead of 15)
 const pr = ["השני ב-50%", "", 0, 2, null, 0.5, ""];
 near(C.promoLine(10, pr, 2), 15);
 near(C.promoLine(10, pr, 3), 25);
 near(C.promoLine(10, ["20% הנחה", "", 0, 1, null, 0.2, ""], 2), 16);
 // legacy percent value still works
 near(C.promoLine(10, ["20% הנחה", "", 0, 1, null, 20, ""], 1), 8);
});

test("promo validity window", () => {
 assert.ok(C.promoOk(["", "2026-11-30", 0, 1, 5, null, "2026-11-01"], T));
 assert.ok(!C.promoOk(["", "2026-11-19", 0, 1, 5, null, ""], T));        // ended yesterday
 assert.ok(!C.promoOk(["", "2026-12-30", 0, 1, 5, null, "2026-11-27"], T)); // not started yet
 assert.ok(C.promoOk(["", "", 0, 1, 5, null, ""], T));
});

test("club detection: numbers and legacy strings", () => {
 assert.ok(!C.isClub(0)); assert.ok(C.isClub(1));
 assert.ok(!C.isClub("0 - כלל הלקוחות")); assert.ok(!C.isClub(""));       // bug: "" counted as club
 assert.ok(!C.isClub("כלל הלקוחות")); assert.ok(!C.isClub(null));
 assert.ok(C.isClub("1=Yellow friends")); assert.ok(C.isClub("(3=פלטינום)"));
});

test("bestIn picks the cheapest candidate and promo, honoring clubs and dates", () => {
 const f = { p: { a: 10, b: [12, null] }, pr: { 1: ["ב-7", "", 1, 1, 7, null, ""], 2: ["ב-9", "2026-11-19", 0, 1, 9, null, ""], 3: ["ב-8", "", 0, 1, 8, null, ""] },
  l: { a: [1, 2], b: [3] } };
 let r = C.bestIn(f, ["a", "b"], 1, false, T);
 assert.strictEqual(r.code, "b"); near(r.v, 8); assert.strictEqual(r.why.d, "ב-8");
 r = C.bestIn(f, ["a", "b"], 1, true, T);
 assert.strictEqual(r.code, "a"); near(r.v, 7); assert.ok(r.why.club);
 r = C.bestIn(f, ["a"], 2, false, T);                     // expired promo ignored
 near(r.v, 20); assert.strictEqual(r.why, null); near(r.reg, 20);
 assert.strictEqual(C.bestIn(f, ["zzz"], 1, false, T), null);
});

test("half-kg quantities", () => {
 near(C.promoLine(10, ["ב-8", "", 0, 1, 8, null, ""], 1.5), 12);
 assert.strictEqual(C.fmtQty(1.5, true), '1.5 ק"ג');
 assert.strictEqual(C.fmtQty(2, true), '2 ק"ג');
 assert.strictEqual(C.kgStep(true), 0.5);
 assert.strictEqual(C.kgStep(false), 1);
});

test("bestPair splits the basket between two stores", () => {
 const M = { s1: { x: { v: 5 }, y: { v: 9 } }, s2: { x: { v: 7 }, y: { v: 4 } }, s3: { x: null, y: { v: 3 } } };
 const bp = C.bestPair(["s1", "s2", "s3"], ["x", "y"], M);
 near(bp.sum, 8); assert.deepStrictEqual([bp.a, bp.b], ["s1", "s3"]);
});

test("pickSub: best-scored substitute the store sells, cheapest among equals", () => {
 const f = { p: { heinz: 15, osem: 12, tomo: 9, other: 8 }, pr: {}, l: {} };
 // top score 1.0: osem and tomo are both sold -> cheapest (tomo); "other" has a lower score and is ignored
 let r = C.pickSub(f, [["missing", 1.0], ["osem", 1.0], ["tomo", 1.0], ["other", 0.7]], 1, false, T);
 assert.strictEqual(r.sub, "tomo"); near(r.v, 9);
 // nothing at the top score is sold -> fall to the next score
 r = C.pickSub(f, [["missing", 1.0], ["other", 0.7]], 2, false, T);
 assert.strictEqual(r.sub, "other"); near(r.v, 16);
 assert.strictEqual(C.pickSub(f, [["missing", 1]], 1, false, T), null);
 assert.strictEqual(C.pickSub(f, undefined, 1, false, T), null);
});

test("unitPrice per 100 g / 100 ml / unit", () => {
 near(C.unitPrice(14.9, [700, "g"]).v, 14.9 / 7);
 assert.strictEqual(C.unitPrice(14.9, [700, "g"]).per, "ל-100 גרם");
 assert.strictEqual(C.unitPrice(9, [1000, "ml"]).per, 'ל-100 מ"ל');
 near(C.unitPrice(30, [30, "u"]).v, 1);
 assert.strictEqual(C.unitPrice(10, null), null);
});

test("pickSub scales the substitute price to the original size", () => {
 // bug found in the UI test: a 500 g mayonnaise won against 620 g just by being smaller
 const f = { p: { small: 9.9, same: 12.5 }, pr: {}, l: {} };
 const z = { orig: [620, "g"], small: [500, "g"], same: [600, "g"] };
 const r = C.pickSub(f, [["small", 1], ["same", 1]], 1, false, T, "orig", z);
 assert.strictEqual(r.sub, "small");                 // 9.90 * 620/500 = 12.28 < 12.50 * 620/600 = 12.92
 near(r.v, 9.9 * 620 / 500); near(r.paid, 9.9); near(r.scale, 1.24);
 const r2 = C.pickSub({ p: { small: 11, same: 12.5 }, pr: {}, l: {} }, [["small", 1], ["same", 1]], 1, false, T, "orig", z);
 assert.strictEqual(r2.sub, "same");                 // 11 * 1.24 = 13.64 > 12.92: the bigger pack is the better deal
 const r3 = C.pickSub(f, [["small", 1]], 1, false, T, "orig", { small: [500, "g"] });
 near(r3.v, 9.9);                                    // unknown original size: no scaling
});

test("missing items are estimated at their average price, never 0", () => {
 // Tomer: an incomplete basket looked cheap because missing items counted as 0
 const M = { a: { x: { v: 10 }, y: { v: 6 } }, b: { x: { v: 12 }, y: null }, c: { x: { v: 14 }, y: { v: 8 } } };
 near(C.avgPrice(M, ["a", "b", "c"], "y"), 7);
 const e = C.estimate(M, ["a", "b", "c"], "b", ["x", "y"]);
 near(e.tot, 12); near(e.est, 7); near(e.total, 19); assert.deepStrictEqual(e.missing, ["y"]);
 // b (12 + ~7 = 19) is no longer cheaper than a (16) just because y is missing
 assert.ok(C.estimate(M, ["a", "b", "c"], "a", ["x", "y"]).total < e.total);
 assert.strictEqual(C.avgPrice({ a: { z: null } }, ["a"], "z"), null);
});

test("pickSub with a merged product as substitute (kg vs unit produce)", () => {
 const f = { p: { u1: 8.9 }, pr: {}, l: {} };
 const expand = c => c === "g:cuc unit" ? ["u1", "u2"] : [c];
 const r = C.pickSub(f, [["g:cuc unit", 0.9]], 1, false, T, "g:cuc", {}, expand);
 assert.strictEqual(r.sub, "g:cuc unit"); near(r.v, 8.9);
 assert.strictEqual(C.pickSub(f, [["g:cuc unit", 0.9]], 1, false, T, "g:cuc", {}), null);  // without expand: not found
});

test("basket import link: round trip, Hebrew group names, validation", () => {
 const b = { "g:תפוחי אדמה": 1.5, "7290000345307": 2 };
 const known = c => c in b || c === "g:בצל";
 assert.deepStrictEqual(C.decodeBasket(C.encodeBasket(b), known), b);
 assert.match(C.encodeBasket(b), /^[A-Za-z0-9_-]+$/);  // safe in a URL fragment
 // unknown codes, bad quantities and "_alias" are dropped; nothing usable -> null
 const bad = C.encodeBasket({ "g:בצל": 1, "999": 1, "g:תפוחי אדמה": "x", "7290000345307": -1, "_alias": 3 });
 assert.deepStrictEqual(C.decodeBasket(bad, c => c !== "999"), { "g:בצל": 1 });
 assert.strictEqual(C.decodeBasket(C.encodeBasket({ "g:בצל": 1000 }), known), null);
 assert.strictEqual(C.decodeBasket("%%%", known), null);
 assert.strictEqual(C.decodeBasket(C.encodeBasket([1, 2]), known), null);
});

test("receipt OCR: barcode/qty columns -> items", () => {
 const cat = new Set(["7290000345307", "5000108478119", "g:גזר", "g:בצל"]);
 const grp = { "7290009000313": "g:גזר", "7290009000900": "g:בצל" };
 const resolve = raw => {
  const d = raw.replace(/\D/g, "");
  for (const x of [d, d[1] !== "0" ? d.slice(1) : ""]) {
   if (x.length < 5) continue;
   for (const c of [x, x.length < 13 ? "729" + x.padStart(10, "0") : null]) if (c && (grp[c] || cat.has(c))) return grp[c] || c;
  }
  return null;
 };
 const pages = [
  { code: [{ t: "1410", y: 380 }, { t: "5000108478119", y: 775 }, { t: "7345307", y: 860 }, { t: ".9000900", y: 950 }, { t: "9000313", y: 2200 }],
    qty: [{ t: "32", y: 385 }, { t: "2", y: 775 }, { t: "1", y: 860 }, { t: "0.79", y: 935 }, { t: "10", y: 1000 }] },
  // the carrot's weight was printed on the next page, above its first barcode
  { code: [{ t: "345307", y: 300 }, { t: "1234567", y: 400 }], qty: [{ t: "1.19", y: 90 }, { t: "3", y: 300 }] },
 ];
 const r = C.receiptItems(pages, resolve);
 assert.deepStrictEqual(r.items, [["5000108478119", 2], ["7290000345307", 1], ["g:בצל", 0.79], ["g:גזר", 1.19], ["7290000345307", 3], ]);
 assert.strictEqual(r.unknown, 1);  // "1234567" (the header's "1410" has too few digits to be a code)
});

test("several receipts -> average per receipt the product appears in", () => {
 const isKg = c => c.startsWith("g:");
 const out = C.avgReceipts([[["a", 4], ["g:x", 1.17], ["c", 2], ["c", 1]], [["a", 2], ["g:x", 1.19], ["b", 1]]], isKg);
 assert.deepStrictEqual(out, { a: 3, "g:x": 1.2, c: 3, b: 1 });
 assert.deepStrictEqual(C.avgReceipts([[["a", 1]], [["a", 2]]], isKg), { a: 2 });  // 1.5 -> 2 (whole units)
 assert.deepStrictEqual(C.avgReceipts([[["g:y", 0.03]]], isKg), { "g:y": 0.1 });
});
