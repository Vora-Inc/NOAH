// Score a validated daily fixture with exact BigInt arithmetic.
//
// Process prior-day obligations, prepaid coverage and catch-up debt; apply
// registration entry credit, decay, holding qualification and the streak.
// Starting treatment lasts through the first spendable-pay day. Normalize
// pay weights with the same deterministic output contract as the Python twin.

import type { Dials, Fixture, MinerInput } from "./fixture.ts";
import { decayOneDay, oneDayDecayFp } from "./decay.ts";
import { isqrt, quantizeWeights } from "./pay.ts";
import { FP_SCALE, multiplier } from "./standard_multiplier.ts";

/** The slot factor is exactly 1 at version one, reserved for attestation. */
const SLOT_FP = FP_SCALE;

export interface MinerResult {
  coldkey: string;
  uid: number | null;
  doorOpen: boolean;
  pciFp: bigint;
  multiplierFp: bigint;
  noahCreditRao: bigint;
  foreignCreditRao: bigint;
  baseFp: bigint;
  streakFp: bigint;
  scoreFp: bigint;
  payWeightFp: bigint;
}

export interface FixtureResult {
  fixture: string;
  evalDay: bigint;
  /** Ordered ascending by uid, null uids last by coldkey. */
  miners: MinerResult[];
  /** Empty when the no-weights fallback applies. */
  weightsU16: Map<number, number>;
}

/** The ramp: streak_fp = 1e9 + floor((ceiling - 1e9) x min(k, ramp) / ramp). */
export function streakFp(run: bigint, dials: Dials): bigint {
  const k = run < dials.streakRampDays ? run : dials.streakRampDays;
  return FP_SCALE + ((dials.streakCeilingFp - FP_SCALE) * k) / dials.streakRampDays;
}


export function needForDay(dials: Dials, payRao: bigint): bigint {
  return (dials.targetFp * payRao) / FP_SCALE;
}

function scoreMiner(m: MinerInput, f: Fixture, decayFp: bigint): MinerResult {
  const d = f.dials;

  // The decayed running totals.
  let N = 0n; // own-token credit
  let F = 0n; // matching credit (invariant F <= N)
  let S = 0n; // entombed; the index's numerator, excluding the entry deposit
  let Pd = 0n; // pay on the spendable basis; the index's denominator
  let totalDeposits = 0n;
  let totalPriorPay = 0n;
  let run = 0n; // consecutive hit days


  let creditReserve = 0n;
  let coverageReserve = 0n;
  let debtReserve = 0n;

  let debtWeight = 0n;

  // evalDay's own bookkeeping, reported in the output.
  let noahCreditRao = 0n;
  let foreignCreditRao = 0n;


  let firstSpendablePayDay: bigint | null = null;

  // The parser guarantees every day in this range is present.
  for (let d2 = m.firstActivityDay; d2 <= f.evalDay; d2++) {
    // Decay one day, THEN add the day's amounts, identically for the base
    // credit totals and for both legs of the index.
    N = decayOneDay(N, decayFp);
    F = decayOneDay(F, decayFp);
    S = decayOneDay(S, decayFp);
    Pd = decayOneDay(Pd, decayFp);
    debtWeight = decayOneDay(debtWeight, decayFp);

    const e = m.days.get(d2)!;

    // The day's pay, on the spendable basis, everywhere below: the pay cap,
    // the index's denominator, and the streak classification.
    // paidLockedArrivalRao touches nothing until it unlocks in a later day's
    // payUnlockedRao.
    const spendablePay = e.paidRao + e.payUnlockedRao;
    if (firstSpendablePayDay === null && spendablePay > 0n) {

      firstSpendablePayDay = d2;
    }


    const priorDay = m.days.get(d2 - 1n);
    let priorSpendablePay: bigint;
    let priorNoFault: boolean;
    if (priorDay === undefined) {
      // No day before firstActivityDay exists in this fixture -- this is
      // d2 === firstActivityDay, with no real prior day to compare
      // against yet. Treated as zero pay, and as no-fault (NEUTRAL rather
      // than MISS): a miner cannot be faulted on a comparison day with no
      // real history behind it at all, the same reasoning starting
      // treatment already applies to this exact gap.
      priorSpendablePay = 0n;
      priorNoFault = true;
    } else {
      priorSpendablePay = priorDay.paidRao + priorDay.payUnlockedRao;
      priorNoFault = priorDay.noFault;
    }

    totalDeposits += e.noahDepositRao;
    totalPriorPay += priorSpendablePay;

    // Own-token credit, capped at day d2-1's spendable pay.
    let credit = e.noahDepositRao < priorSpendablePay ? e.noahDepositRao : priorSpendablePay;


    const need = needForDay(d, priorSpendablePay);

    let draw = 0n;
    let repay = 0n;
    const debtBefore = debtReserve;
    if (priorSpendablePay > 0n && e.noahDepositRao < need) {
      const shortfall = need - e.noahDepositRao;
      draw = coverageReserve < shortfall ? coverageReserve : shortfall;
      coverageReserve -= draw;
      const released = draw < creditReserve ? draw : creditReserve;
      creditReserve -= released;
      credit += released;
      debtReserve += need - e.noahDepositRao - draw;
    } else {

      const cappedNeed = e.noahDepositRao < need ? e.noahDepositRao : need;
      const surplus = e.noahDepositRao - cappedNeed;
      repay = surplus < debtReserve ? surplus : debtReserve;
      debtReserve -= repay;
    }
    // Then this day's own surplus that repay did not use is banked, so a
    // deposit is never used, banked and repaid with more than once.
    creditReserve += e.noahDepositRao - (e.noahDepositRao < priorSpendablePay ? e.noahDepositRao : priorSpendablePay);
    const bankedSurplus = e.noahDepositRao > need ? e.noahDepositRao - need : 0n;
    coverageReserve += bankedSurplus - repay;

    debtWeight += debtReserve > debtBefore ? debtReserve - debtBefore : 0n;
    const creditedRepay = debtBefore > 0n ? (debtWeight * repay) / debtBefore : 0n;
    debtWeight -= creditedRepay;
    N += credit;

    // The entry deposit is credited in the registration day and is exempt
    // from that day's pay cap. It opens matching room from day one.
    // Untouched by the day-d2-1 pairing above: the entry deposit was never
    // part of that mechanism to begin with.
    if (d2 === m.registrationDay) N += m.entryDepositRao;

    // Matching funds: foreign credit up to room = N - F. Unaffected by the
    // noon-schedule pairing above: it matches room opened by own-token
    // credit, same day, as before.
    const room = N - F; // F <= N is an invariant; decay and adds preserve it
    const fCredit = e.foreignDepositNoahEquivRao < room ? e.foreignDepositNoahEquivRao : room;
    F += fCredit;


    S += (e.noahDepositRao < need ? e.noahDepositRao : need) + draw + creditedRepay;
    Pd += priorSpendablePay;

    // Streak classification at day close, against day d2-1's pay and its
    // own no-fault flag. The evaluation day's own classification counts in
    // the run. Lock state does not change it: spendable pay is the pay the
    // classification sees.
    if (priorSpendablePay > 0n) {

      run = e.noahDepositRao + draw >= need ? run + 1n : 0n;
    } else if (!priorNoFault) {
      run = 0n; // paid nothing, and not excused: behavioral miss
    } // else neutral: neither extends nor resets

    if (d2 === f.evalDay) {
      // When the registration day is the evaluation day, the reported
      // credit includes the entry credit.
      noahCreditRao = d2 === m.registrationDay ? credit + m.entryDepositRao : credit;
      foreignCreditRao = fCredit;
    }
  }

  // The door.
  const doorOpen = m.holdRao >= d.ongoingHoldRao;

  // The index. Zero decayed pay gives an index of 0.
  let pciFp = Pd > 0n ? (S * FP_SCALE) / Pd : 0n;
  // Non-expiring surplus may earn a bonus after debt is cleared. Entry,
  // foreign matching and reserve draws are not new regular deposits.
  if (d.capFp > FP_SCALE && debtReserve === 0n && coverageReserve > 0n && totalPriorPay > 0n) {
    const surplusPci = (totalDeposits * FP_SCALE) / totalPriorPay;
    if (surplusPci > d.targetFp) {
      const bounded = surplusPci < (1n << 64n) ? surplusPci : (1n << 64n) - 1n;
      if (bounded > pciFp) pciFp = bounded;
    }
  }


  const inStartingTreatment = firstSpendablePayDay === null || f.evalDay <= firstSpendablePayDay;
  const multiplierFp = inStartingTreatment
    ? FP_SCALE
    : multiplier(pciFp, d.targetFp, d.harshness, d.capFp);

  // The score: base x streak x slot, gated by the door.
  const base = N + F;
  const streak = streakFp(run, d);
  let score = (base * streak) / FP_SCALE;
  score = (score * SLOT_FP) / FP_SCALE;
  if (!doorOpen) score = 0n;

  // The pay weight, before quantization.
  const payWeightFp = (isqrt(score) * multiplierFp) / FP_SCALE;

  return {
    coldkey: m.coldkey,
    uid: m.uid,
    doorOpen,
    pciFp,
    multiplierFp,
    noahCreditRao,
    foreignCreditRao,
    baseFp: base,
    streakFp: streak,
    scoreFp: score,
    payWeightFp,
  };
}

export function scoreFixture(f: Fixture): FixtureResult {
  const decayFp = oneDayDecayFp(f.dials.halfLifeDays);
  const miners = f.miners.map((m) => scoreMiner(m, f, decayFp));

  // Output miners ascending by uid, null uids last by coldkey.
  miners.sort((a, b) => {
    if (a.uid !== null && b.uid !== null) return a.uid - b.uid;
    if (a.uid !== null) return -1;
    if (b.uid !== null) return 1;
    return a.coldkey < b.coldkey ? -1 : a.coldkey > b.coldkey ? 1 : 0;
  });

  // Quantization runs over live UIDs only; a null uid gets no weight slot.
  // Every live uid enters the map, zero weights included.
  const x = new Map<number, bigint>();
  for (const r of miners) {
    if (r.uid !== null) x.set(r.uid, r.payWeightFp);
  }
  const q = quantizeWeights(x);

  return {
    fixture: f.name,
    evalDay: f.evalDay,
    miners,
    weightsU16: q.weightsSet ? q.weights : new Map(),
  };
}

/** Shape the result as the `noah-score-output/6` JSON object. */
export function resultToJson(r: FixtureResult): unknown {
  const weightsU16: Record<string, number> = {};
  for (const [uid, w] of r.weightsU16) weightsU16[String(uid)] = w;
  return {
    schema: "noah-score-output/6",
    fixture: r.fixture,
    evalDay: r.evalDay, // bigint becomes a decimal string in the canonical writer
    miners: r.miners.map((m) => ({
      coldkey: m.coldkey,

      uid: m.uid,
      doorOpen: m.doorOpen,
      pciFp: m.pciFp,
      multiplierFp: m.multiplierFp,
      noahCreditRao: m.noahCreditRao,
      foreignCreditRao: m.foreignCreditRao,
      baseFp: m.baseFp,
      streakFp: m.streakFp,
      scoreFp: m.scoreFp,
      payWeightFp: m.payWeightFp,
    })),
    weightsU16,
  };
}
