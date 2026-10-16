// Regression for the tonight bug: `weightsU16` is `{uid_string: weight}`,
// e.g. `{"1": ..., "2": ..., ..., "36": ...}`. The CONTRACT ("object keys
// sorted lexicographically at every depth", canonical.ts's own top comment)
// requires genuine lexicographic string order ("1","10","11",...,"2","20",...).
//
// The old `canonicalJson` built a correctly-sorted key list with
// `Object.keys(src).sort()`, then assigned those keys onto a plain `{}`
// object one by one before handing that object to `JSON.stringify`. That
// assignment step is where the order was lost: the ECMAScript spec mandates
// that a plain object's own integer-index-like string keys ("1" through
// "120" all qualify) are always enumerated by `Object.keys`/
// `JSON.stringify` in ascending NUMERIC order, regardless of insertion
// order or any prior `.sort()` call. No earlier fixture had 10+ live UIDs,
// so numeric and lexicographic order happened to coincide every time this
// ran before tonight.
//
// These tests fail on the pre-fix writer (verified below by stashing the
// fix and re-running) and pass after it, because the fix writes the
// canonical text directly from the sorted entry list and never re-assigns
// those keys onto a plain object.

import { test } from "node:test";
import assert from "node:assert/strict";

import { canonicalJson } from "../src/canonical.ts";

/** Every "<key>": line at exactly the given indent depth, in the order
 * they appear in the rendered text -- read straight off the bytes, not
 * through any object/Map that could itself reorder integer-like keys. */
function keyOrderAtDepth(text: string, depth: number): string[] {
  const indent = "  ".repeat(depth);
  const re = new RegExp(`^${indent}"([^"]+)":`, "gm");
  const out: string[] = [];
  for (const m of text.matchAll(re)) out.push(m[1]);
  return out;
}

test("weightsU16-shaped object: 120 numeric-string keys render in genuine lexicographic order", () => {
  // Built in ASCENDING NUMERIC insertion order on purpose -- the shape a
  // real day-close weights map arrives in -- so this cannot pass by
  // accident from insertion order alone.
  const weightsU16: Record<string, number> = {};
  for (let uid = 1; uid <= 120; uid++) weightsU16[String(uid)] = uid;

  const text = canonicalJson({ weightsU16 });
  const actual = keyOrderAtDepth(text, 2);

  // Ground truth computed independently of canonicalJson: plain
  // Array.prototype.sort() on strings, which is unaffected by the
  // integer-index-property enumeration quirk (that quirk is specific to
  // Object property enumeration, not Array sorting).
  const expected = Array.from({ length: 120 }, (_, i) => String(i + 1)).sort();

  assert.deepEqual(actual, expected);

  // Pin the three boundaries where numeric and lexicographic order
  // diverge, explicitly, so a future change that breaks just one of them
  // fails loudly and legibly rather than only via the full-array diff.
  const at = (k: string) => actual.indexOf(k);
  assert.ok(at("10") < at("2"), '"10" must sort before "2" (9->10 boundary)');
  assert.ok(at("11") > at("10") && at("11") < at("12"), '"11" must sort between "10" and "12"');
  assert.ok(at("100") < at("99"), '"100" must sort before "99" (99->100 boundary)');
  assert.ok(at("100") > at("10") && at("100") < at("101"), '"100" must sort right after "10"');
});

test("weightsU16-shaped object: exact byte text matches hand-built expectation for a small crossing case", () => {
  // A small, fully-spelled-out case (uids 1, 2, 9, 10, 11) that crosses the
  // 9->10 boundary, checked against literal expected bytes rather than a
  // derived comparison -- catches a spacing/indent regression too, not
  // just an ordering one.
  const weightsU16 = { "1": 100, "2": 200, "9": 900, "10": 1000, "11": 1100 };
  const text = canonicalJson({ weightsU16 });
  assert.equal(
    text,
    [
      "{",
      '  "weightsU16": {',
      '    "1": 100,',
      '    "10": 1000,',
      '    "11": 1100,',
      '    "2": 200,',
      '    "9": 900',
      "  }",
      "}",
      "",
    ].join("\n"),
  );
});

test("nested object under an array element also sorts lexicographically (not just top-level)", () => {
  const perUid: Record<string, number> = {};
  for (const uid of [1, 2, 9, 10, 11, 20, 99, 100, 101]) perUid[String(uid)] = uid;
  const text = canonicalJson({ miners: [{ coldkey: "5F...", perUid }] });
  const actual = keyOrderAtDepth(text, 4);
  const expected = ["1", "10", "100", "101", "11", "2", "20", "9", "99"];
  assert.deepEqual(actual, expected);
});

test("non-numeric keys still sort lexicographically as before (no regression on the ordinary case)", () => {
  const text = canonicalJson({ zeta: 1, alpha: 2, mid: 3 });
  assert.equal(text, ['{', '  "alpha": 2,', '  "mid": 3,', '  "zeta": 1', '}', ''].join("\n"));
});

test("bigint values still render as quoted decimal strings", () => {
  const text = canonicalJson({ evalDay: 5n, uid: 7 });
  assert.equal(text, ['{', '  "evalDay": "5",', '  "uid": 7', '}', ''].join("\n"));
});

test("empty object and empty array render compactly", () => {
  assert.equal(canonicalJson({ weightsU16: {} }), '{\n  "weightsU16": {}\n}\n');
  assert.equal(canonicalJson({ miners: [] }), '{\n  "miners": []\n}\n');
});
