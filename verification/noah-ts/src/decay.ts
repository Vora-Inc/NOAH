// Compute the daily fixed-point decay factor using exact integer search.
// Return floor(1e9 * 2^(-1 / halfLifeDays)); no floating point is used.
// The half-life is supplied by the fixture, and each decay step floors again.

import { FP_SCALE } from "./standard_multiplier.ts";

const cache = new Map<bigint, bigint>();

/** floor(2^(-1/halfLifeDays) x 1e9), exact, BigInt-only. */
export function oneDayDecayFp(halfLifeDays: bigint): bigint {
  if (halfLifeDays < 1n) throw new Error(`half-life must be >= 1, got ${halfLifeDays}`);
  const hit = cache.get(halfLifeDays);
  if (hit !== undefined) return hit;
  const target = FP_SCALE ** halfLifeDays; // (1e9)^H
  // invariant: lo satisfies v^H * 2 <= target, hi + 1 does not
  let lo = 0n;
  let hi = FP_SCALE;
  while (lo < hi) {
    const mid = (lo + hi + 1n) / 2n;
    if (mid ** halfLifeDays * 2n <= target) lo = mid;
    else hi = mid - 1n;
  }
  cache.set(halfLifeDays, lo);
  return lo;
}

/** One day-close decay step on a running total: floor(total x DECAY[1] / 1e9). */
export function decayOneDay(total: bigint, decayFp: bigint): bigint {
  return (total * decayFp) / FP_SCALE;
}
