// Hand-computed worked examples for the daily scoring chain. Expected values
// were calculated independently of this implementation.

import { test } from "node:test";
import assert from "node:assert/strict";

import type { Dials } from "../src/fixture.ts";
import { loadFixture } from "../src/fixture.ts";
import { scoreFixture, streakFp } from "../src/score.ts";

// Test dials. These are arbitrary unit-test settings; a real subnet's dials
// arrive as fixture data and nothing here hardcodes one.
const DIALS = {
  targetFp: "500000000", // ask: half of pay
  harshness: 1,
  capFp: "2000000000",
  halfLifeDays: "182",
  streakCeilingFp: "2000000000",
  streakRampDays: "365",
  ongoingHoldRao: "400000000000",
};

type DayJson = {
  paidRao: string;
  paidLockedArrivalRao: string;
  payUnlockedRao: string;
  noahDepositRao: string;
  foreignDepositNoahEquivRao: string;
  noFault: boolean;
};

type MinerJson = {
  coldkey: string;
  uid: number | null;
  holdRao: string;
  days: Record<string, DayJson>;
  entryDepositRao?: string;
  registrationDay?: string;
  firstActivityDay?: string;
};

function day(paid: string, dep: string, foreign = "0", noFault = false): DayJson {
  return {
    paidRao: paid,
    paidLockedArrivalRao: "0",
    payUnlockedRao: "0",
    noahDepositRao: dep,
    foreignDepositNoahEquivRao: foreign,
    noFault,
  };
}

/** A day whose pay arrived locked: recorded, deferred, in no total yet. */
function lockedDay(locked: string, dep: string, noFault = false): DayJson {
  return { ...day("0", dep, "0", noFault), paidLockedArrivalRao: locked };
}

/** A day in which previously locked pay unlocked. */
function unlockDay(unlocked: string, dep: string, paid = "0"): DayJson {
  return { ...day(paid, dep), payUnlockedRao: unlocked };
}

function fx(evalDay: string, miners: MinerJson[], dials: Record<string, unknown> = DIALS) {
  const full = miners.map((m) => {
    const first =
      m.firstActivityDay ??
      String(Math.min(...Object.keys(m.days).map(Number)));
    return {
      coldkey: m.coldkey,
      uid: m.uid,
      holdRao: m.holdRao,
      firstActivityDay: first,
      entryDepositRao: m.entryDepositRao ?? "0",
      registrationDay: m.registrationDay ?? first,
      days: m.days,
    };
  });
  return loadFixture(JSON.stringify({
    schema: "noah-fixture/6",
    name: "unit",
    dayEpochTs: "1785715200",
    evalDay,
    dials,
    miners: full,
  }));
}

test("single hit day, day one: credit, matching, pci, streak, score, pay", () => {
  // #16/#135: a deposit is judged against the PRIOR day's pay, not its own.
  // Day 4 supplies the pay alone (no deposit yet -- there is no day 3 for
  // day 4's own deposit to be judged against); day 5 (eval) supplies the
  // deposit actually judged here, against day 4's pay. Day 4 is therefore
  // this seat's own first-ever spendable pay day (#112 starting treatment,
  // unshifted -- it still reads each day's own real pay), so day 5 is
  // already past the transition: the multiplier is genuinely measured, not
  // held flat.
  const r = scoreFixture(fx("5", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "4": day("100", "0"), "5": day("0", "60", "30") },
  }]));
  const m = r.miners[0];
  assert.equal(m.doorOpen, true); // holdRao >= dial, boundary inclusive
  assert.equal(m.noahCreditRao, 60n); // min(60, day 4's pay 100)
  assert.equal(m.foreignCreditRao, 30n); // room was 60
  assert.equal(m.baseFp, 90n); // N=60, F=30
  // 60 regular deposits / 100 prior pay; 10 still covers future need.
  // Foreign matching is excluded. Linear test harshness gives 1.2x.
  assert.equal(m.pciFp, 600_000_000n);
  assert.equal(m.multiplierFp, 1_200_000_000n);
  // run=1: streak = 1e9 + floor(1e9 * 1 / 365)
  assert.equal(m.streakFp, 1_002_739_726n);
  // score = floor(90 * 1002739726 / 1e9) = 90, slot no-op, door open
  assert.equal(m.scoreFp, 90n);
  // payWeight = floor(isqrt(90)=9 * 1.2e9 / 1e9) = 10
  assert.equal(m.payWeightFp, 10n);
  assert.deepEqual([...r.weightsU16], [[0, 65535]]);
});

test("pay-cap boundary: deposit exactly equal to the day's pay", () => {
  // #16/#135: day 0 supplies the pay alone; day 1 (eval) supplies the
  // deposit under test, judged against day 0's pay.
  const r = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": day("100", "0"), "1": day("0", "100") },
  }]));
  assert.equal(r.miners[0].noahCreditRao, 100n); // min(100,100): nothing lost
  const r2 = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": day("100", "0"), "1": day("0", "101") },
  }]));
  assert.equal(r2.miners[0].noahCreditRao, 100n); // credit capped at prior pay
  // All 101 regular units enter the bonus ratio once; credit remains capped.
  assert.equal(r2.miners[0].pciFp, 1_010_000_000n);
  assert.equal(r2.miners[0].multiplierFp, 2_000_000_000n);
});

test("matching-room exhaustion: F pinned at N", () => {
  // #16/#135: day 0 supplies the pay alone; day 1 (eval) supplies the
  // deposit and foreign credit under test, judged against day 0's pay.
  const r = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": day("50", "0"), "1": day("0", "50", "1000") },
  }]));
  const m = r.miners[0];
  assert.equal(m.noahCreditRao, 50n);
  assert.equal(m.foreignCreditRao, 50n); // room = N - F = 50, not 1000
  assert.equal(m.baseFp, 100n);
});

test("no NOAH in, no foreign credit: matching funds never substitute", () => {
  const r = scoreFixture(fx("0", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": day("50", "0", "1000") },
  }]));
  const m = r.miners[0];
  assert.equal(m.foreignCreditRao, 0n); // room = 0
  assert.equal(m.baseFp, 0n);
  assert.equal(m.scoreFp, 0n);
});

test("decay then add: half-life 1 halves prior totals at day close", () => {
  // #16/#135: day d's deposit is judged against day d-1's pay. Day 0
  // supplies the pay alone (no deposit yet -- there is no day -1 for its
  // own deposit to be judged against); day 1's deposit is judged against
  // day 0's pay, and day 1's OWN pay in turn becomes day 2's prior pay, so
  // day 2 (eval) still exercises a real decay-then-add day close between
  // two real, credit-bearing comparisons.
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: {
      "0": day("100", "0"),
      "1": day("200", "0"),
      "2": day("0", "100", "50"),
    },
  }], dials));
  const m = r.miners[0];
  // Day 0: no prior day, so prior pay is 0: N=0, S=0, Pd=0, neutral, run=0.
  // Day 1: judged against day 0's pay 100, deposit 0 -> credit 0, N stays 0;
  //   S=0; Pd=0+100=100; miss (0 < need 50), run=0. Day 1's OWN pay (200)
  //   becomes day 2's prior pay.
  // Day 2 close: decay -> N=0, F=0, S=0, Pd=50; credit=min(100,200)=100 ->
  //   N=100; room=100 -> F=50 (of 50 offered, foreign is the binding side
  //   here); S=0+100=100; Pd=50+200=250; need=floor(0.5*200)=100, deposit
  //   100 >= 100 -> hit, run=1.
  assert.equal(m.noahCreditRao, 100n);
  assert.equal(m.foreignCreditRao, 50n);
  assert.equal(m.baseFp, 150n);
  assert.equal(m.pciFp, 400_000_000n); // floor(100e9/250)
  // ratio = floor(pci*1e9/target) = floor(4e8*1e9/5e8) = 8e8, harshness 1,
  // below the cap: genuinely measured and punished (below target).
  assert.equal(m.multiplierFp, 800_000_000n);
  assert.equal(m.streakFp, 1_002_739_726n); // 1e9 + floor(1e9*1/365)
});

test("streak: one behavioral miss resets, noFault days are neutral", () => {
  // #16/#135: a deposit is judged against the PRIOR day's pay, and noFault
  // shifts with it -- it excuses the day WHOSE PAY IS ZERO, which is now
  // the day being read as the prior, not the day being judged. Day 0
  // supplies the pay alone (no deposit -- no day -1 to judge it against).
  const hold = "400000000000";
  // Day 1 hits against day 0's pay (100, need 50, deposit 50). Day 1's OWN
  // pay is 0 (it is a deposit-only day) and its OWN noFault is true -- that
  // is the fact that excuses day 2's judgment (day 2 is judged against day
  // 1's pay 0, day 1's own noFault). Day 2's OWN pay is likewise 0 and
  // excused, carrying the same excuse to day 3.
  const neutral = scoreFixture(fx("3", [{
    coldkey: "A", uid: 0, holdRao: hold,
    days: {
      "0": day("100", "0"),
      "1": day("0", "50", "0", true),
      "2": day("0", "0", "0", true),
      "3": day("0", "0"),
    },
  }]));
  assert.equal(neutral.miners[0].streakFp, 1_002_739_726n); // run=1

  // Same shape, day 1 NOT excused for its own zero pay: day 2 (judged
  // against day 1's pay 0, day 1's own noFault false) is a behavioral
  // miss, run resets to 0.
  const missed = scoreFixture(fx("3", [{
    coldkey: "A", uid: 0, holdRao: hold,
    days: {
      "0": day("100", "0"),
      "1": day("0", "50", "0", false),
      "2": day("0", "0", "0", false),
      "3": day("0", "0"),
    },
  }]));
  assert.equal(missed.miners[0].streakFp, 1_000_000_000n); // run=0

  // Paid but short of the ask: also a reset. Day 1 is judged against day
  // 0's pay 100 (need 50), deposit only 49: a miss, run 0.
  const short = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: hold,
    days: {
      "0": day("100", "0"),
      "1": day("0", "49"), // need floor(0.5*100)=50
    },
  }]));
  assert.equal(short.miners[0].streakFp, 1_000_000_000n); // run=0
});

test("streak ramp reaches the ceiling at the ramp dial and stays there", () => {
  const dials: Dials = {
    targetFp: 500_000_000n,
    harshness: 1,
    capFp: 2_000_000_000n,
    halfLifeDays: 26n,
    streakCeilingFp: 2_000_000_000n,
    streakRampDays: 52n,
    ongoingHoldRao: 400_000_000_000n,
  };
  assert.equal(streakFp(0n, dials), 1_000_000_000n);
  assert.equal(streakFp(26n, dials), 1_500_000_000n);
  assert.equal(streakFp(52n, dials), 2_000_000_000n);
  assert.equal(streakFp(500n, dials), 2_000_000_000n); // capped at the ceiling
});

test("closed door zeroes the score and the pay weight, nothing else", () => {
  // #16/#135: day 0 supplies the pay alone; day 1 (eval) supplies the
  // deposit under test, judged against day 0's pay.
  const r = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "399999999999", // one rao short of the dial
    days: { "0": day("100", "0"), "1": day("0", "60") },
  }]));
  const m = r.miners[0];
  assert.equal(m.doorOpen, false);
  assert.equal(m.scoreFp, 0n);
  assert.equal(m.payWeightFp, 0n);
  assert.equal(m.baseFp, 60n); // credit and history untouched
  assert.equal(m.pciFp, 600_000_000n); // bonus does not bypass the closed door
  assert.equal(r.weightsU16.size, 0); // sole miner gated -> no weights set
});

test("no grace: the old grace boundary no longer changes anything, but starting treatment still applies while never paid", () => {
  // The old grace boundary (age vs halfLifeDays) is gone and changes
  // nothing here. But this wallet is never paid a single rao in either
  // fixture, so #112 starting treatment holds its multiplier at a flat 1.0
  // regardless of age -- the OPPOSITE of the old no-grace behavior (which
  // would have measured pci_fp 0 straight into a multiplier of 0). This is
  // exactly the circularity #112 fixes: a real deposit with zero real pay
  // yet must not be punished as if it had been measured and found wanting.
  const dials = { ...DIALS, halfLifeDays: "2" };
  const quiet = day("0", "0", "0", true);

  // One day of age (1 < 2, the old grace window).
  const younger = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": quiet, "1": quiet },
  }], dials));
  assert.equal(younger.miners[0].pciFp, 0n);
  assert.equal(younger.miners[0].multiplierFp, 1_000_000_000n); // never paid: starting treatment

  // Exactly one memory window of age (2, not < 2, the old grace boundary).
  // Still never paid a single rao, so still starting treatment -- identical
  // to the younger case, with no discontinuity at the old boundary.
  const older = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": quiet, "1": quiet, "2": quiet },
  }], dials));
  assert.equal(older.miners[0].pciFp, 0n);
  assert.equal(older.miners[0].multiplierFp, 1_000_000_000n); // never paid: starting treatment, identical
});

test("entry deposit: credited in the registration day, opens matching room", () => {
  const r = scoreFixture(fx("0", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    entryDepositRao: "100", registrationDay: "0",
    days: { "0": day("0", "0", "50", true) },
  }]));
  const m = r.miners[0];
  // No pay yet: the daily cap would zero any ordinary credit, but the entry
  // deposit is exempt and lands in full; it opens matching room immediately.
  // registrationDay == evalDay, so the OUTPUT includes the entry credit:
  // 0 ordinary + 100 entry.
  assert.equal(m.noahCreditRao, 100n);
  assert.equal(m.foreignCreditRao, 50n); // room came from the entry deposit
  assert.equal(m.baseFp, 150n); // N=100 (entry), F=50
  assert.equal(m.pciFp, 0n); // entry deposit excluded from both index legs
  // Never paid a single rao: #112 starting treatment holds the multiplier
  // at a flat 1.0, not the pci-0-measured value of 0.
  assert.equal(m.multiplierFp, 1_000_000_000n);
  assert.equal(m.streakFp, 1_000_000_000n); // no-fault day: neutral, run 0
  assert.equal(m.scoreFp, 150n);
  assert.equal(m.payWeightFp, 12n); // floor(isqrt(150)=12 x 1.0e9 / 1e9)
});

test("entry deposit: exempt from the pay cap, excluded from both index legs", () => {
  // #16/#135: day -1 does not exist, so day 0 supplies the prior pay (10,
  // no deposit) and registration/eval both land on day 1: ordinary credit
  // there is capped at day 0's pay (10), the entry deposit of 100 lands in
  // full anyway. The bonus ratio includes only 30 regular units / 10 pay.
  // The 100 entry units are excluded; reserve accounting stays unchanged.
  const r = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    entryDepositRao: "100", registrationDay: "1", firstActivityDay: "0",
    days: { "0": day("10", "0"), "1": day("0", "30") },
  }]));
  const m = r.miners[0];
  // registrationDay == evalDay: output = min(30, prior pay 10) + 100 entry.
  assert.equal(m.noahCreditRao, 110n);
  assert.equal(m.baseFp, 110n); // 10 capped credit + 100 entry
  assert.equal(m.pciFp, 3_000_000_000n); // 30/10, not (30+100)/10
});

test("entry credit stays OUT of the output when registrationDay < evalDay", () => {
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("1", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    entryDepositRao: "100", registrationDay: "0",
    days: {
      "0": day("100", "50"),
      "1": day("100", "50"),
    },
  }], dials));
  // The evaluation day is not the registration day: the output field is the
  // plain min(deposit, paid) for the evaluation day.
  assert.equal(r.miners[0].noahCreditRao, 50n);
});

test("at target stays 1x; genuine surplus makes the 2x cap reachable", () => {
  const dials = { ...DIALS, halfLifeDays: "1" };
  for (const [deposit, pci, mult] of [["50", 500_000_000n, 1_000_000_000n],
                                    ["100", 1_000_000_000n, 2_000_000_000n]] as const) {
    const r = scoreFixture(fx("2", [{
      coldkey: "A", uid: 0, holdRao: "400000000000",
      days: { "0": day("100", "0"), "1": day("100", deposit), "2": day("100", deposit) },
    }], dials));
    assert.equal(r.miners[0].pciFp, pci);
    assert.equal(r.miners[0].multiplierFp, mult);
  }
});

test("a cap below 1.0 binds even though pci is well above target", () => {
  // Same pci/ratio as above (1e9, day 2, two steps past the day-0
  // starting-treatment transition), but the cap dial is now 0.5: the cap
  // binds even though pci sits exactly on target.
  const dials = { ...DIALS, halfLifeDays: "1", capFp: "500000000" };
  const r = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": day("100", "0"), "1": day("100", "100"), "2": day("100", "100") },
  }], dials));
  assert.equal(r.miners[0].pciFp, 500_000_000n);
  assert.equal(r.miners[0].multiplierFp, 500_000_000n);
});

test("entry deposit excluded from the index when measured", () => {
  // #16/#135: day 0 (pre-registration) supplies the prior pay alone;
  // registration lands on day 1, with day 2 (eval) as the genuinely
  // measured day two steps past it.
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    entryDepositRao: "200", registrationDay: "1", firstActivityDay: "0",
    days: {
      "0": day("100", "0"),
      "1": day("100", "50"),
      "2": day("100", "50"),
    },
  }], dials));
  const m = r.miners[0];
  // Day 1 (registration): credit min(50, day 0's pay 100)=50 -> N=50+200
  // (entry)=250; S=50; Pd=100.
  // Day 2 close: decay -> N=125, S=25, Pd=50; credit min(50, day 1's pay
  // 100)=50 -> N=175; S=75; Pd=150.
  assert.equal(m.baseFp, 175n);
  assert.equal(m.pciFp, 500_000_000n); // floor(75e9/150), entry never in S
  // Measured (1 - 0 = 1, not < 1): ratio exactly 1.0, harshness 1.
  assert.equal(m.multiplierFp, 1_000_000_000n);
});

test("deregistered miner (uid null) is scored but gets no weight slot", () => {
  // #16/#135: day 0 supplies the pay alone; day 1 (eval) supplies the
  // deposit under test, judged against day 0's pay -- needed here for a
  // genuinely nonzero score to test against.
  const days = { "0": day("100", "0"), "1": day("0", "100") };
  const r = scoreFixture(fx("1", [
    {
      coldkey: "A", uid: null, holdRao: "400000000000",
      days: { ...days },
    },
    {
      coldkey: "B", uid: 3, holdRao: "400000000000",
      days: { ...days },
    },
  ]));
  const dereg = r.miners.find((m) => m.uid === null)!;
  assert.ok(dereg.scoreFp > 0n);
  assert.deepEqual([...r.weightsU16], [[3, 65535]]);
});

test("output order: ascending uid, null uids last by coldkey", () => {
  const w = { "0": day("100", "100") };
  const hold = "400000000000";
  const r = scoreFixture(fx("0", [
    { coldkey: "C", uid: 5, holdRao: hold, days: { ...w } },
    { coldkey: "B", uid: null, holdRao: hold, days: { ...w } },
    { coldkey: "D", uid: 2, holdRao: hold, days: { ...w } },
    { coldkey: "A", uid: null, holdRao: hold, days: { ...w } },
  ]));
  assert.deepEqual(
    r.miners.map((m) => [m.uid, m.coldkey]),
    [[2, "D"], [5, "C"], [null, "A"], [null, "B"]],
  );
});

test("every live uid appears in the weight map, zeros included", () => {
  // #16/#135: day 0 supplies the pay alone; day 1 (eval) supplies the
  // deposit under test, judged against day 0's pay.
  const days = { "0": day("100", "0"), "1": day("0", "100") };
  const r = scoreFixture(fx("1", [
    {
      coldkey: "A", uid: 0, holdRao: "400000000000",
      days: { ...days },
    },
    {
      coldkey: "B", uid: 1, holdRao: "0", // door closed: score and weight zero
      days: { ...days },
    },
  ]));
  assert.equal(r.weightsU16.size, 2);
  assert.equal(r.weightsU16.get(0), 65535);
  assert.equal(r.weightsU16.get(1), 0);
});

test("deferred lock: pay that arrived locked touches no total", () => {
  // Day 0: 100 arrived locked, nothing spendable, deposit 50.
  const r = scoreFixture(fx("0", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": lockedDay("100", "50") },
  }]));
  const m = r.miners[0];
  // The day's pay on the spendable basis is 0: the cap zeroes the credit,
  // and the locked arrival never enters the denominator.
  assert.equal(m.noahCreditRao, 0n); // min(50, 0)
  assert.equal(m.baseFp, 0n);
  assert.equal(m.pciFp, 0n); // Pd stayed 0
  // Classification sees pay 0 and noFault false, so it is a behavioral miss,
  // exactly as if nothing had been paid. Nothing is exempted, only deferred.
  assert.equal(m.streakFp, 1_000_000_000n); // run 0
  assert.equal(m.scoreFp, 0n);

  // The same day excused (noFault true) is neutral, and stays valid: the
  // consistency error keys on spendable pay, which is zero here.
  const excused = scoreFixture(fx("0", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: { "0": lockedDay("100", "0", true) },
  }]));
  assert.equal(excused.miners[0].streakFp, 1_000_000_000n);
});

test("unlock day: the unlock drives the cap, the denominator, and the ask", () => {
  // #16/#135: a deposit is judged against the PRIOR day's spendable pay.
  // Day 0: 100 arrives locked, excused (no deposit -- establishes no real
  // comparison yet). Day 1: the 100 unlocks (own spendable pay 100, no
  // deposit -- becomes day 2's prior pay). Day 2 (eval): the 80 deposit,
  // judged against day 1's unlock-driven spendable pay of 100.
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: {
      "0": lockedDay("100", "0", true),
      "1": unlockDay("100", "0"),
      "2": day("0", "80"),
    },
  }], dials));
  const m = r.miners[0];
  // Day 2: prior spendable pay = day 1's 0 + 100 (unlock). Credit =
  // min(80, 100) = 80, because the unlock opened the cap that a locked
  // arrival could not. Pd = 100.
  assert.equal(m.noahCreditRao, 80n);
  assert.equal(m.baseFp, 80n);
  // 80 regular units / 100 unlocked prior pay; linear test harshness = 1.6x.
  assert.equal(m.pciFp, 800_000_000n);
  assert.equal(m.multiplierFp, 1_600_000_000n);
  // The ask followed the spendable pay: need floor(0.5 x 100) = 50, deposit
  // 80: a hit, run 1.
  assert.equal(m.streakFp, 1_002_739_726n);
  assert.equal(m.scoreFp, 80n); // floor(80 x 1002739726 / 1e9)
  assert.equal(m.payWeightFp, 12n); // floor(isqrt(80)=8 x 1.6e9 / 1e9)
});

test("an unlock adds to same-day spendable pay everywhere", () => {
  // #16/#135: day 0 carries the locked arrival, excused, no deposit yet.
  // Day 1 pays 40 spendable and unlocks 60 more (own spendable pay 100, no
  // deposit -- becomes day 2's prior pay). Day 2 (eval) deposits 100,
  // judged against day 1's spendable pay: 40 + 60 = 100 on every path.
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: {
      "0": lockedDay("60", "0", true),
      "1": unlockDay("60", "0", "40"),
      "2": day("0", "100"),
    },
  }], dials));
  const m = r.miners[0];
  assert.equal(m.noahCreditRao, 100n); // min(100, 40 + 60): nothing lost
  assert.equal(m.pciFp, 1_000_000_000n); // regular 100 / unlocked prior pay 100
  assert.equal(m.streakFp, 1_002_739_726n); // need 50, deposit 100: hit, run 1
});

test("classification: a short deposit against unlocked pay is a miss", () => {
  // #16/#135: day 0 carries real pay and a locked arrival, no deposit yet
  // (it has no prior day of its own, so it is neutral regardless). Day 1's
  // pay is all unlock (paidRao 0), judged against day 0's real pay; day 1's
  // own spendable pay (all unlock) in turn judges day 2 (eval). Deposit 49
  // against need 50 on day 2 is a miss; day 1 itself is a hit either way
  // (its own deposit judged against day 0's real, unlocked pay of 100).
  const missed = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: {
      "0": { ...day("100", "0"), paidLockedArrivalRao: "100" },
      "1": unlockDay("100", "50"),
      "2": day("0", "49"),
    },
  }]));
  assert.equal(missed.miners[0].streakFp, 1_000_000_000n); // day 1 hit, day 2 miss: run 0

  const hit = scoreFixture(fx("2", [{
    coldkey: "A", uid: 0, holdRao: "400000000000",
    days: {
      "0": { ...day("100", "0"), paidLockedArrivalRao: "100" },
      "1": unlockDay("100", "50"),
      "2": day("0", "50"),
    },
  }]));
  // Day 1: judged against day 0's real pay 100, deposit 50 -- hit, run 1.
  // Day 2 (eval): judged against day 1's spendable pay (all unlock) 100,
  // deposit 50 -- hit, run 2. (One fewer real comparison than a same-day
  // engine would have had over the same 3 days: day 0 itself has no prior
  // day of its own to be judged against, and is neutral instead.)
  assert.equal(hit.miners[0].streakFp, 1_005_479_452n); // run 2
});

test("multiplier separates miners once measured", () => {
  // Half-life 1 so both miners are measured at evalDay 2. #16/#135: day 0
  // supplies each miner's pay alone (no deposit yet); days 1 and 2 then
  // each deposit against the immediately preceding day's pay. #290: the
  // index counts a deposit only up to the day's need, so PCI cannot exceed
  // the target. A deposits exactly the need (5000 of 10000 pay): pci 0.5 =
  // target, multiplier exactly 1.0. B deposits half its need with foreign
  // matching on top (5000 of 20000 pay, need 10000): pci 0.25, ratio 0.5,
  // multiplier 0.5. B's base is larger, yet the multiplier tilts pay to A.
  const dials = { ...DIALS, halfLifeDays: "1" };
  const r = scoreFixture(fx("2", [
    {
      coldkey: "A", uid: 0, holdRao: "400000000000",
      days: {
        "0": day("10000", "0"),
        "1": day("10000", "5000"),
        "2": day("10000", "5000"),
      },
    },
    {
      coldkey: "B", uid: 1, holdRao: "400000000000",
      days: {
        "0": day("20000", "0"),
        "1": day("20000", "5000", "5000"),
        "2": day("20000", "5000", "5000"),
      },
    },
  ], dials));
  const [a, b] = r.miners;
  // A: N = 5000/2 + 5000 = 7500; S = 7500, Pd = 15000: pci 0.5 -> ratio 1.0.
  // B: N = 7500, F = 7500, base 15000; S = 7500, Pd = 30000: pci 0.25 ->
  // ratio 0.5 -> multiplier 0.5.
  assert.equal(a.pciFp, 500_000_000n);
  assert.equal(b.pciFp, 250_000_000n);
  assert.equal(a.multiplierFp, 1_000_000_000n);
  assert.equal(b.multiplierFp, 500_000_000n);
  // A hit on both days (run 2, streak 1005479452); B missed both (run 0).
  // a.score = floor(7500 x 1005479452 / 1e9) = 7541; isqrt = 86; pay 86.
  // b.score = 15000; isqrt = 122; pay floor(122 x 0.5) = 61.
  assert.equal(a.payWeightFp, 86n);
  assert.equal(b.payWeightFp, 61n);
  const wa = r.weightsU16.get(0) ?? 0;
  const wb = r.weightsU16.get(1) ?? 0;
  assert.equal(wa + wb, 65535);
  assert.ok(wa > wb);
});

// --- the prepaid contribution reserve (#290) --------------------------------
//
// One miner, pay 100 every day, target 0.9 (need 90). Dials chosen so the run
// length reads straight back from the streak: ceiling 101.0 over a 100-day
// ramp gives streak = 1e9 + 1e9 x run.

const RUN_DIALS = {
  targetFp: "900000000",
  harshness: 2,
  capFp: "1000000000",
  halfLifeDays: "45",
  streakCeilingFp: "101000000000",
  streakRampDays: "100",
  ongoingHoldRao: "0",
};

/** A miner paid `pay` every day; deposits[i] is day i+1's deposit. */
function paidMiner(deposits: number[], pay = 100, noPay: number[] = [], noFault: number[] = []) {
  const days: Record<string, DayJson> = {};
  deposits.forEach((dep, i) => {
    const d = i + 1;
    days[String(d)] = day(noPay.includes(d) ? "0" : String(pay), String(dep), "0", noFault.includes(d));
  });
  return { coldkey: "P", uid: 0, holdRao: "0", days };
}

/** The run length on eval day `ev`, read from the streak factor. */
function runOn(deposits: number[], ev: number, noPay: number[] = [], noFault: number[] = []): bigint {
  const r = scoreFixture(fx(String(ev), [paidMiner(deposits.slice(0, ev), 100, noPay, noFault)], RUN_DIALS));
  return (r.miners[0].streakFp - 1_000_000_000n) / 1_000_000_000n;
}

test("prepaid: a skipped day with no reserve is still a miss", () => {
  const deps = [0, 90, 90, 0, 90];
  assert.equal(runOn(deps, 3), 2n);
  assert.equal(runOn(deps, 4), 0n); // nothing banked, nothing drawn
  assert.equal(runOn(deps, 5), 1n);
});

test("prepaid: three days paid in advance cover exactly three days", () => {
  // Day 2 deposits 4 x 90: its own need and three in advance. Days 3..5 draw
  // 90 each; day 6 has nothing left.
  const deps = [0, 360, 0, 0, 0, 0];
  assert.deepEqual([2, 3, 4, 5, 6].map((e) => runOn(deps, e)), [1n, 2n, 3n, 4n, 0n]);
});

test("prepaid: a reserve short by ten draws all it has and then misses", () => {
  // Day 2: 170 = need 90 + 80 banked. Day 3: draws 80 against a need of 90.
  const deps = [0, 170, 0, 0];
  assert.deepEqual([2, 3, 4].map((e) => runOn(deps, e)), [1n, 0n, 0n]);
});

test("prepaid: an excused zero-pay day is neutral and draws nothing", () => {
  // Pay on days 1-3 and 5-7, none on day 4 (excused). Day 2 deposits 360.
  // d2 HIT 1 (coverage 270), d3 2 (180), d4 3 (90), d5 pairs day 4's excused
  // zero pay: neutral, 3, no draw (90), d6 draws the last 90: 4, d7 misses.
  const deps = [0, 360, 0, 0, 0, 0, 0];
  const got = [2, 3, 4, 5, 6, 7].map((e) => runOn(deps, e, [4], [4]));
  assert.deepEqual(got, [1n, 2n, 3n, 3n, 4n, 0n]);
});

test("prepaid: an unexcused zero-pay day is a miss even with a reserve", () => {
  const deps = [0, 450, 0, 0];
  assert.equal(runOn(deps, 3, [3]), 2n); // day 3 pairs day 2's pay: draws, hit
  assert.equal(runOn(deps, 4, [3]), 0n); // day 4 pairs day 3's zero pay: miss
});

test("prepaid: a day-1 deposit (no pay to pair) is banked and covers day 3", () => {
  // Day 1 has no pay: its 90 goes to both reserves, credits nothing now.
  // Day 2 pairs with day 1's zero pay: not judged as a miss only if excused;
  // here it is unexcused, so a miss. Day 3 pairs with day 2's pay 100: the
  // banked 90 is drawn, a hit, and credited as 90.
  const dials = RUN_DIALS;
  const m = paidMiner([90, 0, 0], 100, [1]);
  const at = (ev: number) => scoreFixture(fx(String(ev), [
    { ...m, days: Object.fromEntries(Object.entries(m.days).filter(([k]) => Number(k) <= ev)) },
  ], dials)).miners[0];
  assert.equal(at(1).noahCreditRao, 0n);
  assert.equal(at(2).noahCreditRao, 0n);
  assert.equal(at(3).noahCreditRao, 90n);
  assert.equal(at(3).streakFp, 2_000_000_000n); // run 1
});

test("prepaid: miner P (1,800 on day 2, then silence) scores like ordinary O by day 21", () => {
  // Both earn 100 a day; target 0.9; entry deposit 10 NOAH. Values below are
  // the Python engine's exact integers for the same fixture, which agree
  // with the table Task Lead specified (P 202.6 / 1913.9 / 4320.2).
  const dials = { ...RUN_DIALS, streakCeilingFp: "2000000000", streakRampDays: "90" };
  const N = 1_000_000_000;
  const build = (deps: number[]) => {
    const days: Record<string, DayJson> = {};
    deps.forEach((dep, i) => { days[String(i + 1)] = day(String(100 * N), String(dep * N)); });
    return { coldkey: "P", uid: 0, holdRao: "0", entryDepositRao: String(10 * N), days };
  };
  const p = Array.from({ length: 45 }, (_, i) => (i === 0 ? 0 : i === 1 ? 1800 : i < 21 ? 0 : 90));
  const o = Array.from({ length: 45 }, (_, i) => (i === 0 ? 0 : 90));
  const score = (deps: number[], ev: number) =>
    scoreFixture(fx(String(ev), [build(deps.slice(0, ev))], dials)).miners[0];
  for (const [ev, ps, os] of [
    [3, 202_571_842_286n, 192_505_869_268n],
    [21, 1_913_903_598_683n, 1_917_004_691_566n],
    [45, 4_320_221_238_650n, 4_322_831_461_257n],
  ] as const) {
    const a = score(p, ev), b = score(o, ev);
    assert.equal(a.scoreFp, ps);
    assert.equal(b.scoreFp, os);
    assert.equal(a.pciFp, 900_000_000n); // at target
    assert.equal(a.multiplierFp, 1_000_000_000n); // never penalised
    assert.equal(a.streakFp, b.streakFp); // same unbroken run as ordinary
  }
});

test("surplus bonus draws do not recount receipts and cap1 keeps legacy PCI", () => {
  const dials = { ...DIALS, harshness: 2, halfLifeDays: "45", ongoingHoldRao: "0" };
  const days = { "0": day("100", "120"), "1": day("100", "0"),
                 "2": day("100", "0"), "3": day("100", "0") };
  const input = [{ coldkey: "A", uid: 0, holdRao: "0", days }];
  const firstThree = [{ ...input[0], days: { "0": days["0"], "1": days["1"], "2": days["2"] } }];
  const r = scoreFixture(fx("2", firstThree, dials)).miners[0];
  assert.equal(r.pciFp, 600_000_000n); // 120/200, no second count of draws
  assert.equal(r.multiplierFp, 1_440_000_000n);
  const later = scoreFixture(fx("3", input, dials)).miners[0];
  const oldLater = scoreFixture(fx("3", input, { ...dials, capFp: "1000000000" })).miners[0];
  assert.deepEqual(later, oldLater); // unpaid debt keeps the aged penalty
  const legacy = scoreFixture(fx("2", firstThree, { ...dials, capFp: "1000000000" })).miners[0];
  assert.ok(legacy.pciFp <= 500_000_000n);
  assert.equal(legacy.scoreFp, r.scoreFp);
});

test("unlimited prepaid surplus saturates multiplier input without overflow", () => {
  const dials = { ...DIALS, harshness: 2, ongoingHoldRao: "0" };
  const days = { "0": day("1", "10000000000000000000000000000000000000000"), "1": day("0", "0") };
  const input = [{ coldkey: "A", uid: 0, holdRao: "0", days }];
  const first = scoreFixture(fx("0", [{ ...input[0], days: { "0": days["0"] } }], dials)).miners[0];
  assert.equal(first.multiplierFp, 1_000_000_000n);
  const r = scoreFixture(fx("1", input, dials)).miners[0];
  assert.equal(r.pciFp, (1n << 64n) - 1n);
  assert.equal(r.multiplierFp, 2_000_000_000n);
});
