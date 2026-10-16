// Strict `noah-fixture/6` validation. Unknown fields
// are an error, not a warning, and so are missing fields. On top of that:
// the day-enumeration errors, the noFault/paidRao consistency error, and the
// cumulative-unlock consistency error on the lock-arrival and unlock fields.

import { test } from "node:test";
import assert from "node:assert/strict";

import { loadFixture } from "../src/fixture.ts";

function base(): Record<string, unknown> {
  return {
    schema: "noah-fixture/6",
    name: "unit",
    dayEpochTs: "1785715200",
    evalDay: "5",
    dials: {
      targetFp: "500000000",
      harshness: 1,
      capFp: "2000000000",
      halfLifeDays: "182",
      streakCeilingFp: "2000000000",
      streakRampDays: "365",
      ongoingHoldRao: "400000000000",
    },
    miners: [
      {
        coldkey: "5Fexample",
        uid: 7,
        holdRao: "412000000000",
        firstActivityDay: "5",
        entryDepositRao: "0",
        registrationDay: "5",
        days: {
          "5": {
            paidRao: "80000000000",
            paidLockedArrivalRao: "0",
            payUnlockedRao: "0",
            noahDepositRao: "40000000000",
            foreignDepositNoahEquivRao: "0",
            noFault: false,
          },
        },
      },
    ],
  };
}

const load = (o: unknown) => loadFixture(JSON.stringify(o));
const miner0 = (o: Record<string, unknown>) =>
  (o.miners as unknown[])[0] as Record<string, unknown>;

test("accepts the contract's example shape", () => {
  const f = load(base());
  assert.equal(f.name, "unit");
  assert.equal(f.evalDay, 5n);
  assert.equal(f.miners.length, 1);
  assert.equal(f.miners[0].uid, 7);
  assert.equal(f.miners[0].firstActivityDay, 5n);
  assert.equal(f.miners[0].entryDepositRao, 0n);
  assert.equal(f.miners[0].registrationDay, 5n);
  assert.equal(f.miners[0].days.get(5n)?.paidRao, 80_000_000_000n);
});

test("rejects a wrong or missing schema", () => {
  const o = base();
  o.schema = "noah-fixture/2";
  assert.throws(() => load(o), /schema/);
  delete o.schema;
  assert.throws(() => load(o), /schema/);
});

test("rejects fractional and exponential JSON number literals", () => {
  // Python's json module keeps 1e2/1.0 as float, and _int_field's
  // isinstance(int) check rejects it; a naive JSON.parse in TypeScript
  // cannot tell "1e2" apart from "100" post-parse (issue #523). The literal
  // must survive as raw text into loadFixture -- round-tripping through
  // JSON.stringify first would silently canonicalize it back to "100".
  const withExponent = JSON.stringify(base()).replace('"evalDay":"5"', '"evalDay":5e0');
  assert.throws(() => loadFixture(withExponent), /fractional or exponential/);

  const withDecimalPoint = JSON.stringify(base()).replace('"harshness":1', '"harshness":1.0');
  assert.throws(() => loadFixture(withDecimalPoint), /fractional or exponential/);
});

test("accepts plain JSON integers and canonical decimal strings for the same field", () => {
  const withPlainInt = base();
  withPlainInt.evalDay = 5;
  const f = load(withPlainInt);
  assert.equal(f.evalDay, 5n);
});

// A bare JSON number literal above Number.MAX_SAFE_INTEGER (2**53 - 1):
// JSON.parse itself would already have silently rounded such a literal
// before big()/smallInt() (this file's own module) ever see it, so both
// refuse it outright -- and, under ticket #10 (a real codex adversarial
// finding), noah/scoring/fixture_io.py's own Python parser now refuses the
// identical shape too, closing a real cross-engine input-acceptance
// divergence its own arbitrary-precision int used to paper over silently.
// Pinned here so this side of that agreement cannot silently regress.
test("rejects a bare JSON int above JavaScript's own safe integer bound", () => {
  const o = base();
  const days = miner0(o).days as Record<string, Record<string, unknown>>;
  days["5"].paidRao = 2 ** 53; // one past Number.MAX_SAFE_INTEGER (2**53 - 1)
  assert.throws(() => load(o), /expected non-negative integer/);
});

test("accepts exactly the safe integer bound itself as a bare literal", () => {
  const o = base();
  const days = miner0(o).days as Record<string, Record<string, unknown>>;
  days["5"].paidRao = Number.MAX_SAFE_INTEGER;
  const f = load(o);
  assert.equal(f.miners[0].days.get(5n)?.paidRao, BigInt(Number.MAX_SAFE_INTEGER));
});

test("accepts the identical value above the bound as a canonical decimal string", () => {
  // The safe-integer bound applies only to a BARE number literal -- the
  // canonical decimal STRING form (BigInt, this module's own big()) stays
  // unconstrained on both engines, exactly as before this fix.
  const o = base();
  const days = miner0(o).days as Record<string, Record<string, unknown>>;
  const tooBig = BigInt(Number.MAX_SAFE_INTEGER) + 1n;
  days["5"].paidRao = tooBig.toString();
  const f = load(o);
  assert.equal(f.miners[0].days.get(5n)?.paidRao, tooBig);
});

test("rejects a name outside the restricted character set", () => {
  // cli.ts joins fixture.name straight into an output filename, so a
  // path-traversal-shaped name (e.g. "../../etc/passwd") must be rejected
  // here rather than reaching the filesystem (issue #524).
  const o = base();
  o.name = "../../etc/passwd";
  assert.throws(() => load(o), /expected a name matching/);
});

test("accepts a name using only the restricted character set", () => {
  const o = base();
  o.name = "day-5.rehearsal_01";
  const f = load(o);
  assert.equal(f.name, "day-5.rehearsal_01");
});

test("rejects unknown fields at every level", () => {
  let o = base();
  (o as Record<string, unknown>).extra = 1;
  assert.throws(() => load(o), /unknown field "extra"/);

  o = base();
  (o.dials as Record<string, unknown>).bonusFp = "1";
  assert.throws(() => load(o), /unknown field "bonusFp"/);

  o = base();
  miner0(o).hotkey = "5G";
  assert.throws(() => load(o), /unknown field "hotkey"/);

  o = base();
  const wk = (miner0(o).days as Record<string, unknown>)["5"] as Record<string, unknown>;
  wk.sweepRao = "1";
  assert.throws(() => load(o), /unknown field "sweepRao"/);
});

test("rejects missing required fields, the new miner fields included", () => {
  let o = base();
  delete (o.dials as Record<string, unknown>).ongoingHoldRao;
  assert.throws(() => load(o), /missing required field "ongoingHoldRao"/);

  for (const field of ["firstActivityDay", "entryDepositRao", "registrationDay"]) {
    o = base();
    delete miner0(o)[field];
    assert.throws(() => load(o), new RegExp(`missing required field "${field}"`));
  }

  for (const field of ["noFault", "paidLockedArrivalRao", "payUnlockedRao"]) {
    o = base();
    const wk = (miner0(o).days as Record<string, unknown>)["5"] as Record<string, unknown>;
    delete wk[field];
    assert.throws(() => load(o), new RegExp(`missing required field "${field}"`));
  }
});

test("cumulative payUnlockedRao may never exceed cumulative paidLockedArrivalRao", () => {
  // An unlock with no locked arrival ever recorded is an error.
  let o = base();
  let wk = (miner0(o).days as Record<string, unknown>)["5"] as Record<string, unknown>;
  wk.payUnlockedRao = "1";
  assert.throws(() => load(o), /cumulative payUnlockedRao.*exceeds cumulative paidLockedArrivalRao/);

  // The check is cumulative THROUGH each day, not day by day: a day-3
  // arrival of 100 covers a day-5 unlock of 100.
  o = base();
  miner0(o).firstActivityDay = "3";
  miner0(o).registrationDay = "3";
  const days = miner0(o).days as Record<string, unknown>;
  const quiet = () => ({
    paidRao: "0", paidLockedArrivalRao: "0", payUnlockedRao: "0",
    noahDepositRao: "0", foreignDepositNoahEquivRao: "0", noFault: true,
  });
  days["3"] = { ...quiet(), paidLockedArrivalRao: "100" };
  days["4"] = quiet();
  wk = days["5"] as Record<string, unknown>;
  wk.payUnlockedRao = "100";
  assert.equal(load(o).miners[0].days.get(5n)?.payUnlockedRao, 100n);

  // One rao more than ever arrived locked, and it fails at the day the
  // running total first goes over.
  wk.payUnlockedRao = "101";
  assert.throws(() => load(o), /through day 5: a token cannot unlock before it arrived locked/);

  // Order matters: unlocking BEFORE the locked arrival is an error even when
  // the lifetime totals balance.
  o = base();
  miner0(o).firstActivityDay = "4";
  miner0(o).registrationDay = "4";
  const days2 = miner0(o).days as Record<string, unknown>;
  days2["4"] = { ...quiet(), payUnlockedRao: "50" };
  days2["5"] = { ...(days2["5"] as Record<string, unknown>), paidLockedArrivalRao: "50" };
  assert.throws(() => load(o), /through day 4: a token cannot unlock before it arrived locked/);
});

test("an absent day in firstActivityDay..evalDay is an error", () => {
  const o = base();
  miner0(o).firstActivityDay = "3";
  const days = miner0(o).days as Record<string, unknown>;
  days["3"] = days["5"];
  // day 4 absent between firstActivityDay 3 and evalDay 5
  assert.throws(() => load(o), /absent day 4/);
});

test("a day outside firstActivityDay..evalDay is an error", () => {
  // Before firstActivityDay.
  let o = base();
  let days = miner0(o).days as Record<string, unknown>;
  days["4"] = days["5"];
  assert.throws(() => load(o), /outside firstActivityDay\.\.evalDay/);

  // Beyond evalDay.
  o = base();
  days = miner0(o).days as Record<string, unknown>;
  days["6"] = days["5"];
  assert.throws(() => load(o), /outside firstActivityDay\.\.evalDay/);
});

test("rejects a firstActivityDay beyond evalDay, naming the rule", () => {
  const o = base();
  miner0(o).firstActivityDay = "6";
  delete (miner0(o).days as Record<string, unknown>)["5"];
  assert.throws(() => load(o), /firstActivityDay beyond evalDay is a validation error/);
});

test("a registrationDay outside range with a nonzero entry deposit is an error", () => {
  // Before firstActivityDay.
  let o = base();
  miner0(o).entryDepositRao = "100";
  miner0(o).registrationDay = "4"; // range is 5..5
  assert.throws(() => load(o), /registrationDay.*nonzero entryDepositRao/);

  // Beyond evalDay.
  o = base();
  miner0(o).entryDepositRao = "100";
  miner0(o).registrationDay = "6";
  assert.throws(() => load(o), /registrationDay.*nonzero entryDepositRao/);

  // Zero entry deposit: the same out-of-range registrationDay stays valid.
  o = base();
  miner0(o).registrationDay = "4"; // entryDepositRao is "0" in base()
  assert.equal(load(o).miners[0].registrationDay, 4n);

  // Nonzero entry deposit with registrationDay in range stays valid.
  o = base();
  miner0(o).entryDepositRao = "100";
  assert.equal(load(o).miners[0].entryDepositRao, 100n);
});

test("noFault true with nonzero paidRao is an error", () => {
  const o = base();
  const wk = (miner0(o).days as Record<string, unknown>)["5"] as Record<string, unknown>;
  wk.noFault = true; // paidRao is nonzero in base()
  assert.throws(() => load(o), /noFault true with nonzero paidRao/);

  // noFault true with zero pay stays valid.
  wk.paidRao = "0";
  assert.equal(load(o).miners[0].days.get(5n)?.noFault, true);
});

test("uid null is allowed (deregistered this day)", () => {
  const o = base();
  miner0(o).uid = null;
  assert.equal(load(o).miners[0].uid, null);
});

test("rejects duplicate uids", () => {
  const o = base();
  const m2 = JSON.parse(JSON.stringify((o.miners as unknown[])[0]));
  m2.coldkey = "5Fother";
  (o.miners as unknown[]).push(m2);
  assert.throws(() => load(o), /duplicate uid/);
});

test("rejects non-integer amounts and bad day keys", () => {
  let o = base();
  const wk = (miner0(o).days as Record<string, unknown>)["5"] as Record<string, unknown>;
  wk.paidRao = "12.5";
  assert.throws(() => load(o), /paidRao/);

  o = base();
  const days = miner0(o).days as Record<string, unknown>;
  days["05"] = days["5"];
  delete days["5"];
  assert.throws(() => load(o), /bad day key/);
});

test("rejects dials the formulas cannot accept", () => {
  let o = base();
  (o.dials as Record<string, unknown>).targetFp = "0";
  assert.throws(() => load(o), /targetFp/);

  o = base();
  (o.dials as Record<string, unknown>).harshness = 9;
  assert.throws(() => load(o), /harshness/);

  o = base();
  (o.dials as Record<string, unknown>).streakRampDays = "0";
  assert.throws(() => load(o), /streakRampDays/);

  o = base();
  (o.dials as Record<string, unknown>).streakCeilingFp = "999999999";
  assert.throws(() => load(o), /streakCeilingFp/);
});

// ---- duplicate uid validation, and D-138 reopened for duplicate coldkeys --
//
// loadFixture still rejects a duplicate uid: it is the per-seat identity
// both engines key their output on, and stays unique across the whole
// fixture regardless of how many rows a coldkey has. D-138 itself is
// reopened (issue #112, 2026-09-19): "Noah recognises and scores exactly one
// hotkey per coldkey" no longer holds -- up to 32 hotkeys may share one
// coldkey, each scored independently -- so a coldkey appearing in more than
// one row of miners is ordinary input again, not a shape this parser
// refuses. No replacement cross-seat validation is added in its place, by
// Drew's own direction: every row is still validated independently by every
// other rule, and there is deliberately no rule comparing two rows of the
// same coldkey against each other.

test("a duplicate uid across two different coldkeys still throws", () => {
  const bad = base();
  const other = JSON.parse(JSON.stringify((bad.miners as unknown[])[0]));
  other.coldkey = "5FdifferentColdkey"; // a DIFFERENT coldkey, same uid (7)
  (bad.miners as unknown[]).push(other);
  assert.throws(() => load(bad), /duplicate uid/);
});

test("a duplicate coldkey with distinct uids now parses and scores", () => {
  // D-138 reopened (issue #112): two live seats for one coldkey -- "the
  // several-seats case" the 2026-08-30 ruling used to close -- is legitimate
  // input again. It must parse AND keep both seats independently, neither
  // one silently dropped or merged into the other.
  const o = base();
  const second = JSON.parse(JSON.stringify((o.miners as unknown[])[0]));
  second.uid = 8; // same coldkey as the first miner, a DIFFERENT uid
  (o.miners as unknown[]).push(second);
  const f = load(o);
  assert.equal(f.miners.length, 2);
  assert.deepEqual(f.miners.map((m) => m.coldkey), [f.miners[0].coldkey, f.miners[0].coldkey]);
  assert.deepEqual(f.miners.map((m) => m.uid).sort(), [f.miners[0].uid, 8].sort());
});

test("a coldkey with a numbered row and a null row now parses fine", () => {
  // The second shape #193 used to allow -- one live seat plus a vacated
  // seat's null-uid row for the SAME coldkey -- is likewise legitimate again
  // under D-138 reopened (issue #112): a coldkey may have a live seat's own
  // row and a separate row parking history no live seat claims, and this
  // parser draws no distinction between them beyond each row's own rules.
  const o = base();
  const nullRow = JSON.parse(JSON.stringify((o.miners as unknown[])[0]));
  nullRow.uid = null;
  nullRow.holdRao = "0";
  nullRow.days["5"].paidRao = "12345";
  (o.miners as unknown[]).push(nullRow);
  const f = load(o);
  assert.deepEqual(f.miners.map((m) => m.uid), [f.miners[0].uid, null]);
});

test("a second null row for one coldkey now parses fine", () => {
  // Two null-uid rows for one coldkey are likewise two rows for one
  // coldkey, and D-138 reopened (issue #112) lifts the whole-coldkey refusal
  // that used to catch this shape along with every other duplicate-coldkey
  // one. No replacement rule is added for it (Drew's own direction: no
  // cross-seat validation in its place).
  const o = base();
  const firstNull = JSON.parse(JSON.stringify((o.miners as unknown[])[0]));
  firstNull.uid = null;
  firstNull.holdRao = "0";
  const secondNull = JSON.parse(JSON.stringify((o.miners as unknown[])[0]));
  secondNull.uid = null;
  secondNull.holdRao = "0";
  (o.miners as unknown[]) = [firstNull, secondNull];
  const f = load(o);
  assert.deepEqual(f.miners.map((m) => m.uid), [null, null]);
});

test("a coldkey with only one row parses fine", () => {
  // The single-row case is unaffected either way it stood: exactly one row
  // for a coldkey has always parsed, before D-138, under it, and now that it
  // is reopened.
  const f = load(base());
  assert.equal(f.miners.length, 1);
});
