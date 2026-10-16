// Strict types and validation for noah-fixture/6.
// Require complete daily history and valid integer amounts; reject unknown
// fields and unsupported schemas. Multiple seats may share a coldkey, while
// each fixture row retains its own qualification and holding inputs.

import { FP_SCALE, HARSHNESS_MAX } from "./standard_multiplier.ts";

export interface DayEntry {
  /** Miner pay that arrived SPENDABLE this day, in rao. */
  paidRao: bigint;
  /** Pay that arrived LOCKED this day: recorded, deferred, in no total yet. */
  paidLockedArrivalRao: bigint;
  /** Previously locked pay that UNLOCKED this day: it enters the denominator and the pay cap now. */
  payUnlockedRao: bigint;
  /** Own-token alpha sent to the Ark this day, in rao. */
  noahDepositRao: bigint;
  /** Other subnets' tokens, already converted to own-token units upstream, in rao. */
  foreignDepositNoahEquivRao: bigint;
  /** Upstream verdict: paid nothing through no fault of the miner. */
  noFault: boolean;
}

export interface MinerInput {

  coldkey: string;
  /** null means no live UID this epoch: the history is scored, but no weight slot. */
  uid: number | null;
  /** What THIS SEAT holds at the read height, in rao — not the wallet's other seats. */
  holdRao: bigint;
  /** The first day with any attributed pay or deposit. */
  firstActivityDay: bigint;
  /** The one-time entry deposit, in rao. 0n when not applicable. */
  entryDepositRao: bigint;
  /** The day the entry deposit lands in. */
  registrationDay: bigint;
  /** Per-day history: every day firstActivityDay..evalDay, no gaps. */
  days: Map<bigint, DayEntry>;
}

export interface Dials {
  targetFp: bigint;
  harshness: number;
  capFp: bigint;
  halfLifeDays: bigint;
  streakCeilingFp: bigint;
  streakRampDays: bigint;
  ongoingHoldRao: bigint;
}

export interface Fixture {
  name: string;
  dayEpochTs: bigint;
  evalDay: bigint;
  dials: Dials;
  miners: MinerInput[];
}

// ---------- JSON loading / strict validation ----------

type J = Record<string, unknown>;

/**
 * A fixture violates the contract. Reject; never guess.
 *
 * `code` is an optional, stable machine-readable tag for the small set of
 * refusals the twin proof itself checks agreement on across both
 * implementations. Free-text messages are never compared between the two
 * engines -- they are independently written and not expected to phrase a
 * refusal identically -- but confirming two independent engines refused the
 * SAME input for the SAME reason needs something narrower than prose. Most
 * refusals carry no code at all; only the ones the twin proof itself
 * exercises need one.
 */
export class FixtureError extends Error {
  readonly code?: string;
  constructor(message: string, code?: string) {
    super(message);
    this.name = "FixtureError";
    this.code = code;
  }
}

function fail(path: string, msg: string, code?: string): never {
  throw new FixtureError(`fixture invalid at ${path}: ${msg}`, code);
}

/** Non-negative integer supplied as a decimal string or a safe JSON number. */
function big(v: unknown, path: string): bigint {
  if (typeof v === "string") {
    if (!/^(0|[1-9]\d*)$/.test(v)) {
      fail(path, `expected non-negative decimal integer string, got ${JSON.stringify(v)}`);
    }
    return BigInt(v);
  }
  if (typeof v === "number" && Number.isSafeInteger(v) && v >= 0) return BigInt(v);
  fail(path, `expected non-negative integer (string or safe number), got ${JSON.stringify(v)}`);
}

function smallInt(v: unknown, path: string): number {
  if (typeof v === "number" && Number.isSafeInteger(v) && v >= 0) return v;
  fail(path, `expected small non-negative integer, got ${JSON.stringify(v)}`);
}

function bool(v: unknown, path: string): boolean {
  if (typeof v === "boolean") return v;
  fail(path, `expected boolean, got ${JSON.stringify(v)}`);
}

function str(v: unknown, path: string): string {
  if (typeof v === "string" && v.length > 0) return v;
  fail(path, `expected non-empty string, got ${JSON.stringify(v)}`);
}

// The fixture name becomes a filename verbatim (cli.ts joins it straight into
// an output path), so it is restricted to the same character set the Python
// implementation already enforces (noah/scoring/fixture_io.py) rather than
// merely required to be a non-empty string.
const NAME_RE = /^[A-Za-z0-9._-]+$/;

function safeName(v: unknown, path: string): string {
  if (typeof v === "string" && NAME_RE.test(v)) return v;
  fail(path, `expected a name matching ${NAME_RE.source}, got ${JSON.stringify(v)}`);
}

function obj(v: unknown, path: string): J {
  if (typeof v === "object" && v !== null && !Array.isArray(v)) return v as J;
  fail(path, "expected object");
}

function arr(v: unknown, path: string): unknown[] {
  if (Array.isArray(v)) return v;
  fail(path, "expected array");
}

/** Contract rule: unknown fields are an error. Missing required keys too. */
function exactKeys(o: J, required: readonly string[], path: string): void {
  for (const k of required) {
    if (!(k in o)) fail(path, `missing required field "${k}"`);
  }
  for (const k of Object.keys(o)) {
    if (!required.includes(k)) fail(path, `unknown field "${k}"`);
  }
}

const ROOT_KEYS = ["schema", "name", "dayEpochTs", "evalDay", "dials", "miners"] as const;
const DIAL_KEYS = [
  "targetFp", "harshness", "capFp", "halfLifeDays",
  "streakCeilingFp", "streakRampDays", "ongoingHoldRao",
] as const;
const MINER_KEYS = [
  "coldkey", "uid", "holdRao",
  "firstActivityDay", "entryDepositRao", "registrationDay",
  "days",
] as const;
const DAY_KEYS = [
  "paidRao", "paidLockedArrivalRao", "payUnlockedRao",
  "noahDepositRao", "foreignDepositNoahEquivRao", "noFault",
] as const;

function parseDials(v: unknown, path: string): Dials {
  const o = obj(v, path);
  exactKeys(o, DIAL_KEYS, path);
  const d: Dials = {
    targetFp: big(o.targetFp, `${path}.targetFp`),
    harshness: smallInt(o.harshness, `${path}.harshness`),
    capFp: big(o.capFp, `${path}.capFp`),
    halfLifeDays: big(o.halfLifeDays, `${path}.halfLifeDays`),
    streakCeilingFp: big(o.streakCeilingFp, `${path}.streakCeilingFp`),
    streakRampDays: big(o.streakRampDays, `${path}.streakRampDays`),
    ongoingHoldRao: big(o.ongoingHoldRao, `${path}.ongoingHoldRao`),
  };
  // Sanity bounds: a dial set the multiplier or streak formulas cannot accept
  // is a fixture we do not fully understand, so it is rejected (contract rule).
  if (d.targetFp <= 0n || d.targetFp > FP_SCALE) fail(`${path}.targetFp`, "must be in (0, 1e9]");
  if (d.harshness < 1 || d.harshness > HARSHNESS_MAX) {
    fail(`${path}.harshness`, `must be an integer in 1..${HARSHNESS_MAX}`);
  }
  if (d.halfLifeDays < 1n) fail(`${path}.halfLifeDays`, "must be >= 1");
  if (d.streakCeilingFp < FP_SCALE) fail(`${path}.streakCeilingFp`, "must be >= 1e9");
  if (d.streakRampDays < 1n) fail(`${path}.streakRampDays`, "must be >= 1");
  return d;
}

function parseDayEntry(v: unknown, path: string): DayEntry {
  const o = obj(v, path);
  exactKeys(o, DAY_KEYS, path);
  const e: DayEntry = {
    paidRao: big(o.paidRao, `${path}.paidRao`),
    paidLockedArrivalRao: big(o.paidLockedArrivalRao, `${path}.paidLockedArrivalRao`),
    payUnlockedRao: big(o.payUnlockedRao, `${path}.payUnlockedRao`),
    noahDepositRao: big(o.noahDepositRao, `${path}.noahDepositRao`),
    foreignDepositNoahEquivRao: big(o.foreignDepositNoahEquivRao, `${path}.foreignDepositNoahEquivRao`),
    noFault: bool(o.noFault, `${path}.noFault`),
  };
  // A no-fault flag alongside nonzero pay is a validation error.
  if (e.noFault && e.paidRao > 0n) {
    fail(path, `noFault true with nonzero paidRao (${e.paidRao})`);
  }
  return e;
}

function parseMiner(v: unknown, path: string, evalDay: bigint): MinerInput {
  const o = obj(v, path);
  exactKeys(o, MINER_KEYS, path);
  const uid = o.uid === null ? null : smallInt(o.uid, `${path}.uid`);
  const firstActivityDay = big(o.firstActivityDay, `${path}.firstActivityDay`);
  if (firstActivityDay > evalDay) {
    fail(`${path}.firstActivityDay`, `beyond evalDay ${evalDay}: a firstActivityDay beyond evalDay is a validation error`);
  }
  const days = new Map<bigint, DayEntry>();
  for (const [dk, dv] of Object.entries(obj(o.days, `${path}.days`))) {
    if (!/^(0|[1-9]\d*)$/.test(dk)) fail(`${path}.days`, `bad day key ${JSON.stringify(dk)}`);
    const d = BigInt(dk);
    // The enumeration is exactly firstActivityDay..evalDay.
    if (d < firstActivityDay || d > evalDay) {
      fail(`${path}.days.${dk}`, `day outside firstActivityDay..evalDay (${firstActivityDay}..${evalDay})`);
    }
    days.set(d, parseDayEntry(dv, `${path}.days.${dk}`));
  }
  // An absent day in range is a validation error, never an implied miss and
  // never an implied neutral day.
  for (let d = firstActivityDay; d <= evalDay; d++) {
    if (!days.has(d)) fail(`${path}.days`, `absent day ${d} (every day ${firstActivityDay}..${evalDay} must be present)`);
  }
  // Cumulative unlocks may never exceed cumulative locked arrivals up to that
  // day. A token cannot unlock before it arrived locked.
  let lockedCum = 0n;
  let unlockedCum = 0n;
  for (let d = firstActivityDay; d <= evalDay; d++) {
    const e = days.get(d)!;
    lockedCum += e.paidLockedArrivalRao;
    unlockedCum += e.payUnlockedRao;
    if (unlockedCum > lockedCum) {
      fail(
        `${path}.days.${d}`,
        `cumulative payUnlockedRao (${unlockedCum}) exceeds cumulative paidLockedArrivalRao (${lockedCum}) through day ${d}: a token cannot unlock before it arrived locked`,
      );
    }
  }
  const entryDepositRao = big(o.entryDepositRao, `${path}.entryDepositRao`);
  const registrationDay = big(o.registrationDay, `${path}.registrationDay`);
  // A registrationDay outside firstActivityDay..evalDay with a nonzero
  // entry deposit is a validation error, never silent.
  if (
    entryDepositRao > 0n &&
    (registrationDay < firstActivityDay || registrationDay > evalDay)
  ) {
    fail(
      `${path}.registrationDay`,
      `outside firstActivityDay..evalDay (${firstActivityDay}..${evalDay}) with nonzero entryDepositRao`,
    );
  }
  return {
    coldkey: str(o.coldkey, `${path}.coldkey`),
    uid,
    holdRao: big(o.holdRao, `${path}.holdRao`),
    firstActivityDay,
    entryDepositRao,
    registrationDay,
    days,
  };
}

// Every fixture number is either a plain JSON integer or a
// canonical decimal string -- a fractional or exponential JSON number literal
// (1.0, 1e2) is never valid anywhere in this schema. Plain JSON.parse cannot
// tell "1e2" apart from "100": both become the identical JS number 100, so a
// naive parse accepted fixtures the Python engine already rejects (Python's
// json module keeps int and float distinct, and _int_field's isinstance(int)
// check throws on the float). This reviver uses JSON.parse's source-text
// reviver argument (standard since Node 21; this project requires Node
// >=22.6) to see the original literal and reject the non-canonical form
// before it reaches any field validator (issue #523).
const _CANONICAL_NUMBER = /^-?(0|[1-9]\d*)$/;
type ReviverContext = { source?: string };
type SourceAwareReviver = (this: unknown, key: string, value: unknown, context: ReviverContext) => unknown;

const _strictNumberReviver: SourceAwareReviver = (key, value, context) => {
  if (typeof value === "number" && !_CANONICAL_NUMBER.test(context.source ?? "")) {
    throw new Error(
      `fixture invalid: "${key}" is ${context.source}, a fractional or exponential JSON number ` +
        `literal -- every fixture number must be a plain JSON integer or a canonical decimal ` +
        `string (noah-fixture/6)`,
    );
  }
  return value;
};

function parseStrict(jsonText: string): unknown {
  return JSON.parse(jsonText, _strictNumberReviver as Parameters<typeof JSON.parse>[1]);
}

export function loadFixture(jsonText: string): Fixture {
  const root = obj(parseStrict(jsonText), "$");
  exactKeys(root, ROOT_KEYS, "$");
  if (root.schema !== "noah-fixture/6") {
    fail("$.schema", `expected "noah-fixture/6", got ${JSON.stringify(root.schema)}`);
  }
  const evalDay = big(root.evalDay, "$.evalDay");
  const miners = arr(root.miners, "$.miners").map((mv, mi) =>
    parseMiner(mv, `$.miners[${mi}]`, evalDay),
  );
  // uid stays unique across the whole fixture regardless: it is the
  // per-seat identity both engines key their output on, and a coldkey may
  // now legitimately hold several live seats (D-138 reopened), so this is
  // the one structural rule that still applies both within one coldkey's
  // own several rows and across different coldkeys.
  const seen = new Set<number>();
  for (const [i, m] of miners.entries()) {
    if (m.uid === null) continue;
    if (seen.has(m.uid)) fail(`$.miners[${i}].uid`, `duplicate uid ${m.uid}`);
    seen.add(m.uid);
  }
  return {
    name: safeName(root.name, "$.name"),
    dayEpochTs: big(root.dayEpochTs, "$.dayEpochTs"),
    evalDay,
    dials: parseDials(root.dials, "$.dials"),
    miners,
  };
}
