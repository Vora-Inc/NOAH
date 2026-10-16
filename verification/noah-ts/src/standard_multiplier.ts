/** Exact fixed-point multiplier: min((PCI / target)^harshness, cap).
 * BigInt arithmetic floors at each division; the cliff is a separate hard gate. */
export const FP_SCALE = 1_000_000_000n;
export const HARSHNESS_MAX = 8;
export const CLIFF = "cliff" as const;

export type Harshness = number | typeof CLIFF;

const U64 = 1n << 64n;

function checkInputs(pci: bigint, target: bigint, harshness: Harshness, cap: bigint): void {
  if (pci < 0n || pci >= U64) throw new RangeError(`pci out of range: ${pci}`);
  if (target <= 0n || target > FP_SCALE) throw new RangeError(`target out of range: ${target}`);
  if (cap < 0n || cap >= U64) throw new RangeError(`cap out of range: ${cap}`);
  if (harshness !== CLIFF) {
    if (!Number.isInteger(harshness) || harshness < 1 || harshness > HARSHNESS_MAX) {
      throw new RangeError(`harshness out of range: ${harshness}`);
    }
  }
}

/** The cliff: a hard gate. The cap is ignored at the cliff. */
function cliff(pci: bigint, target: bigint, _cap: bigint): bigint {
  if (pci < target) return 0n;
  return FP_SCALE;
}

/**
 * min((pci/target)^harshness, cap), floor at every step, FP_SCALE-scaled.
 */
export function multiplier(pci: bigint, target: bigint, harshness: Harshness, cap: bigint): bigint {
  checkInputs(pci, target, harshness, cap);

  if (harshness === CLIFF) return cliff(pci, target, cap);

  // ratio = pci / target, floored.
  const ratio = (pci * FP_SCALE) / target;

  if (ratio >= FP_SCALE) {
    // Powers are non-decreasing: the cap decides as soon as it is reached.
    if (ratio >= cap) return cap;
    let acc = ratio;
    for (let i = 1; i < harshness; i++) {
      acc = (acc * ratio) / FP_SCALE;
      if (acc >= cap) return cap;
    }
    return acc; // acc < cap here
  }

  // Powers are non-increasing and stay below FP_SCALE.
  let acc = ratio;
  for (let i = 1; i < (harshness as number); i++) {
    acc = (acc * ratio) / FP_SCALE;
    if (acc === 0n) break; // floor has hit zero; further powers stay zero
  }
  return acc < cap ? acc : cap;
}

/**
 * final score = own score x multiplier, floored. own_score keeps whatever
 * integer scale the adopting subnet already uses.
 */
export function applyMultiplier(ownScore: bigint, mult: bigint): bigint {
  if (ownScore < 0n || ownScore >= U64) throw new RangeError(`own_score out of range: ${ownScore}`);
  if (mult < 0n || mult >= U64) throw new RangeError(`mult out of range: ${mult}`);
  return (ownScore * mult) / FP_SCALE;
}
