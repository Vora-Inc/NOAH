/**
 * Vector tests for the Standard's multiplier.
 *
 * Runs the identical vector file the Python implementation runs. The canonical
 * copy lives at noah/standard/tests/vectors/multiplier_vectors.json, and the
 * copy in fixtures/ here must be byte-identical to it. The hash check below
 * enforces that, and skips itself if the canonical copy is not present.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { multiplier, applyMultiplier, CLIFF, type Harshness } from "../src/standard_multiplier.ts";

const here = dirname(fileURLToPath(import.meta.url));
const localVectors = join(here, "..", "fixtures", "multiplier_vectors.json");
const canonicalVectors = join(
  here, "..", "..", "..", "noah", "standard", "tests", "vectors", "multiplier_vectors.json",
);

const doc = JSON.parse(readFileSync(localVectors, "utf8"));

function h(raw: string | number): Harshness {
  return raw === "cliff" ? CLIFF : (raw as number);
}

test("multiplier vectors match the canonical set", () => {
  for (const v of doc.multiplier_vectors) {
    const got = multiplier(BigInt(v.pci), BigInt(v.target), h(v.harshness), BigInt(v.cap));
    assert.equal(got, BigInt(v.expected_multiplier), v.name);
  }
});

test("apply vectors match the canonical set", () => {
  for (const v of doc.apply_vectors) {
    const got = applyMultiplier(BigInt(v.own_score), BigInt(v.mult));
    assert.equal(got, BigInt(v.expected), v.name);
  }
});

test("local vector copy is byte-identical to the canonical one", (t) => {
  if (!existsSync(canonicalVectors)) {
    t.skip("the canonical vector file is not present");
    return;
  }
  const a = createHash("sha256").update(readFileSync(localVectors)).digest("hex");
  const b = createHash("sha256").update(readFileSync(canonicalVectors)).digest("hex");
  assert.equal(a, b, "fixtures/multiplier_vectors.json has drifted from the canonical copy");
});

test("input validation", () => {
  assert.throws(() => multiplier(1n, 0n, 1, 1n), RangeError); // zero target
  assert.throws(() => multiplier(1n, 1_000_000_001n, 1, 1n), RangeError); // target above 1.0
  assert.throws(() => multiplier(1n, 1n, 0, 1n), RangeError); // harshness 0
  assert.throws(() => multiplier(1n, 1n, 9, 1n), RangeError); // harshness over max
  assert.throws(() => multiplier(1n << 64n, 1n, 1, 1n), RangeError); // pci over bound
});
