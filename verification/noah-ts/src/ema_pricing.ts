// Independent exact-rational conversion. Prices within each pair share a scale;
// the EMA scale need not equal the spot scale. Runtime decoding is authenticated
// separately; these arithmetic functions never select or guess a chain field.
export function emaCredit(amount: bigint, emaAsset: bigint, emaNative: bigint,
                          spotAsset: bigint, spotNative: bigint): bigint {
  if (typeof amount !== 'bigint' || amount < 0n ||
      [emaAsset, emaNative, spotAsset, spotNative].some(p => typeof p !== 'bigint' || p <= 0n)) {
    throw new Error('invalid EMA/spot conversion inputs');
  }
  // Compare complete ratios, not independently minimized prices. There is one
  // final division: no intermediate TAO rounding and no floating point.
  return emaAsset * spotNative <= spotAsset * emaNative
    ? amount * emaAsset / emaNative
    : amount * spotAsset / spotNative;
}

export type PriceFraction = { numerator: string; denominator: string };
export function priceFraction(value: unknown): [bigint, bigint] {
  if (value === null || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).sort().join(',') !== 'denominator,numerator') {
    throw new Error('invalid rational price fields');
  }
  const p = value as PriceFraction;
  if (typeof p.numerator !== 'string' || !/^(0|[1-9][0-9]*)$/.test(p.numerator) ||
      typeof p.denominator !== 'string' || !/^[1-9][0-9]*$/.test(p.denominator)) {
    throw new Error('invalid rational price integers');
  }
  return [BigInt(p.numerator), BigInt(p.denominator)];
}
export function emaFractionCredit(amount: bigint, emaAsset: unknown, emaNative: unknown,
                                  spotAsset: unknown, spotNative: unknown): bigint {
  const [ea, ead] = priceFraction(emaAsset), [en, end] = priceFraction(emaNative);
  const [pa, pad] = priceFraction(spotAsset), [pn, pnd] = priceFraction(spotNative);
  return emaCredit(amount, ea * end, ead * en, pa * pnd, pad * pn);
}
