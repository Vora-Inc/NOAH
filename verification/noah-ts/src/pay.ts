// Score to pay.
//
//   x(i) = floor( isqrt(score(i, e)) x multiplier_fp(i) / 1e9 )
//
// The square root compresses the top of the distribution, so a miner ten times
// larger does not receive ten times the weight.
//
// The weight vector is the largest-remainder quantization of x to u16: each
// miner gets floor(x(i) x 65535 / sum_j x(j)), residual units go one each to
// the largest remainders, ties broken by lowest UID, and the vector sums to
// exactly 65,535. If sum_j x(j) = 0, no weights are set at all and the
// validator's own fallback applies; the vector is never redirected to a UID.
// This module just reports weightsSet = false.

export interface QuantizeResult {
  /** false means validators set no weights at all for this epoch. */
  weightsSet: boolean;
  /** uid → u16 weight, present only when weightsSet; sums to 65,535. */
  weights: Map<number, number>;
}

/** Exact integer square root (floor), Newton's method on BigInt. */
export function isqrt(n: bigint): bigint {
  if (n < 0n) throw new Error("isqrt of negative value");
  if (n < 2n) return n;
  let x = n;
  let y = (x + 1n) / 2n;
  while (y < x) {
    x = y;
    y = (x + n / x) / 2n;
  }
  return x;
}

const U16_TOTAL = 65_535n;

/**
 * u16 largest-remainder quantization of the pre-quantization weights x(i).
 * Ties broken by lowest UID. Sums to exactly 65,535.
 */
export function quantizeWeights(x: ReadonlyMap<number, bigint>): QuantizeResult {
  const entries = [...x.entries()]
    .map(([uid, w]) => ({ uid, w, u16: 0 }))
    .sort((a, b) => a.uid - b.uid);
  let total = 0n;
  for (const e of entries) {
    if (e.w < 0n) throw new Error(`negative weight for uid ${e.uid}`);
    total += e.w;
  }
  if (total === 0n) return { weightsSet: false, weights: new Map() };

  let assigned = 0n;
  const remainders = entries.map((e) => {
    const prod = e.w * U16_TOTAL;
    const q = prod / total;
    e.u16 = Number(q);
    assigned += q;
    return { e, rem: prod % total };
  });
  let residual = U16_TOTAL - assigned;
  // Largest remainder first; ties broken by lowest UID.
  remainders.sort((a, b) =>
    a.rem === b.rem ? a.e.uid - b.e.uid : a.rem > b.rem ? -1 : 1,
  );
  for (const r of remainders) {
    if (residual === 0n) break;
    r.e.u16 += 1;
    residual -= 1n;
  }
  return { weightsSet: true, weights: new Map(entries.map((e) => [e.uid, e.u16])) };
}
