// isqrt and the u16 largest-remainder quantization.

import { test } from "node:test";
import assert from "node:assert/strict";

import { isqrt, quantizeWeights } from "../src/pay.ts";

test("isqrt exact anchors and floor behavior", () => {
  assert.equal(isqrt(0n), 0n);
  assert.equal(isqrt(1n), 1n);
  assert.equal(isqrt(3n), 1n);
  assert.equal(isqrt(4n), 2n);
  assert.equal(isqrt(99n), 9n);
  assert.equal(isqrt(100n), 10n);
  assert.equal(isqrt(10n ** 18n), 10n ** 9n);
  assert.equal(isqrt(10n ** 18n - 1n), 10n ** 9n - 1n);
});

test("isqrt property on large values", () => {
  for (const n of [12345678901234567890n, 2n ** 96n + 12345n, 999999999999999999999n]) {
    const r = isqrt(n);
    assert.ok(r * r <= n);
    assert.ok((r + 1n) * (r + 1n) > n);
  }
});

test("quantization sums to exactly 65535", () => {
  const q = quantizeWeights(new Map([[0, 3n], [1, 7n], [2, 11n], [5, 1n]]));
  assert.ok(q.weightsSet);
  const sum = [...q.weights.values()].reduce((a, b) => a + b, 0);
  assert.equal(sum, 65535);
});

test("ties go to the lowest UID", () => {
  // Two equal weights: 65535/2 leaves one residual unit; uid 3 must get it.
  const q = quantizeWeights(new Map([[9, 5n], [3, 5n]]));
  assert.ok(q.weightsSet);
  assert.equal(q.weights.get(3), 32768);
  assert.equal(q.weights.get(9), 32767);
});

test("all-zero input sets no weights", () => {
  const q = quantizeWeights(new Map([[0, 0n], [1, 0n]]));
  assert.equal(q.weightsSet, false);
  assert.equal(q.weights.size, 0);
});

test("single miner takes the whole vector", () => {
  const q = quantizeWeights(new Map([[4, 123456789n]]));
  assert.ok(q.weightsSet);
  assert.equal(q.weights.get(4), 65535);
});

test("zero-weight miners keep a zero entry, never a residual unit ahead of earners", () => {
  const q = quantizeWeights(new Map([[0, 0n], [1, 1n], [2, 1n]]));
  assert.ok(q.weightsSet);
  assert.equal(q.weights.get(0), 0);
  assert.equal((q.weights.get(1) ?? 0) + (q.weights.get(2) ?? 0), 65535);
  // 65535 is odd: the tie residual goes to uid 1, the lower of the two earners.
  assert.equal(q.weights.get(1), 32768);
});
