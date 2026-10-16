"""Validate and serialize the noah-fixture/6 scoring contract.

Amounts use decimal integer strings and explicit day/seat identities. Reject
unsupported schema, malformed values and incomplete required history before
scoring. Canonical output supports independent implementation comparisons."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..standard.multiplier import FP_SCALE as MULT_FP_SCALE
from ..standard.multiplier import HARSHNESS_MAX

SCHEMA_INPUT = "noah-fixture/6"
SCHEMA_OUTPUT = "noah-score-output/6"

_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

_JS_MAX_SAFE_INTEGER = 2**53 - 1


class FixtureError(ValueError):
    """A fixture violates the contract. Reject; never guess.

    ``code`` is an optional, stable machine-readable tag for the small set
    of refusals the twin proof itself checks agreement on across both
    implementations. Free text
    in the message is never compared between the two engines -- they are
    independently written and are not expected to phrase a refusal
    identically -- but a caller that needs to confirm two independent
    engines refused the SAME input for the SAME reason needs something
    narrower than prose to compare. Most refusals carry no code at all;
    only the ones the twin proof itself exercises need one.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _require_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise FixtureError(f"{where}: unknown field(s) {sorted(unknown)}")
    missing = allowed - set(obj)
    if missing:
        raise FixtureError(f"{where}: missing field(s) {sorted(missing)}")


def _int_field(value, where: str) -> int:
    """A contract integer: a plain int or a canonical decimal string, >= 0."""
    if isinstance(value, bool):
        raise FixtureError(f"{where}: expected integer, got boolean")
    if isinstance(value, int):
        n = value
        if n > _JS_MAX_SAFE_INTEGER:
            raise FixtureError(
                f"{where}: {n} is a bare JSON number literal above JavaScript's own safe "
                f"integer bound ({_JS_MAX_SAFE_INTEGER}, this module's own docstring: 'written "
                f"as a decimal string when it can exceed 2^53'). verification/noah-ts's own "
                f"fixture parser refuses this exact shape (JSON.parse itself would already "
                f"have silently rounded it), so accepting it here would let the two engines "
                f"disagree on whether this fixture is even legal input, not merely on its "
                f"score. Use the canonical decimal STRING form instead ({str(n)!r}), which "
                f"both engines accept at any size."
            )
    elif isinstance(value, str):
        if not _DECIMAL.fullmatch(value):
            raise FixtureError(f"{where}: not a canonical decimal string: {value!r}")
        n = int(value)
    else:
        raise FixtureError(f"{where}: expected integer or decimal string")
    if n < 0:
        raise FixtureError(f"{where}: negative value {n}")
    return n


@dataclass(frozen=True)
class Dials:
    target_fp: int
    harshness: int
    cap_fp: int
    half_life_days: int
    streak_ceiling_fp: int
    streak_ramp_days: int
    ongoing_hold_rao: int


@dataclass(frozen=True)
class DayEntry:
    paid_rao: int
    paid_locked_arrival_rao: int
    pay_unlocked_rao: int
    noah_deposit_rao: int
    foreign_deposit_noah_equiv_rao: int
    no_fault: bool


@dataclass(frozen=True)
class MinerInput:
    coldkey: str
    uid: int | None
    hold_rao: int
    first_activity_day: int
    entry_deposit_rao: int
    registration_day: int
    days: dict[int, DayEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class Fixture:
    name: str
    day_epoch_ts: int
    eval_day: int
    dials: Dials
    miners: tuple[MinerInput, ...]


def _parse_dials(obj, where: str = "dials") -> Dials:
    if not isinstance(obj, dict):
        raise FixtureError(f"{where}: expected object")
    _require_keys(
        obj,
        {
            "targetFp",
            "harshness",
            "capFp",
            "halfLifeDays",
            "streakCeilingFp",
            "streakRampDays",
            "ongoingHoldRao",
        },
        where,
    )
    harshness = obj["harshness"]
    if isinstance(harshness, bool) or not isinstance(harshness, int):
        raise FixtureError(f"{where}.harshness: must be a plain JSON integer")
    if not (1 <= harshness <= HARSHNESS_MAX):
        raise FixtureError(f"{where}.harshness: out of range: {harshness}")
    target_fp = _int_field(obj["targetFp"], f"{where}.targetFp")
    if not (0 < target_fp <= MULT_FP_SCALE):
        raise FixtureError(f"{where}.targetFp: out of range: {target_fp}")
    half_life = _int_field(obj["halfLifeDays"], f"{where}.halfLifeDays")
    if half_life < 1:
        raise FixtureError(f"{where}.halfLifeDays: must be >= 1")
    ceiling = _int_field(obj["streakCeilingFp"], f"{where}.streakCeilingFp")
    if ceiling < MULT_FP_SCALE:
        raise FixtureError(f"{where}.streakCeilingFp: below 1.0: {ceiling}")
    ramp = _int_field(obj["streakRampDays"], f"{where}.streakRampDays")
    if ramp < 1:
        raise FixtureError(f"{where}.streakRampDays: must be >= 1")
    return Dials(
        target_fp=target_fp,
        harshness=harshness,
        cap_fp=_int_field(obj["capFp"], f"{where}.capFp"),
        half_life_days=half_life,
        streak_ceiling_fp=ceiling,
        streak_ramp_days=ramp,
        ongoing_hold_rao=_int_field(obj["ongoingHoldRao"], f"{where}.ongoingHoldRao"),
    )


def _parse_day(obj, where: str) -> DayEntry:
    if not isinstance(obj, dict):
        raise FixtureError(f"{where}: expected object")
    _require_keys(
        obj,
        {
            "paidRao",
            "paidLockedArrivalRao",
            "payUnlockedRao",
            "noahDepositRao",
            "foreignDepositNoahEquivRao",
            "noFault",
        },
        where,
    )
    if not isinstance(obj["noFault"], bool):
        raise FixtureError(f"{where}.noFault: must be a boolean")
    paid_rao = _int_field(obj["paidRao"], f"{where}.paidRao")
    if obj["noFault"] and paid_rao > 0:
        # A no-fault flag alongside nonzero pay is a validation error.
        raise FixtureError(f"{where}: noFault true with nonzero paidRao")
    return DayEntry(
        paid_rao=paid_rao,
        paid_locked_arrival_rao=_int_field(
            obj["paidLockedArrivalRao"], f"{where}.paidLockedArrivalRao"
        ),
        pay_unlocked_rao=_int_field(obj["payUnlockedRao"], f"{where}.payUnlockedRao"),
        noah_deposit_rao=_int_field(obj["noahDepositRao"], f"{where}.noahDepositRao"),
        foreign_deposit_noah_equiv_rao=_int_field(
            obj["foreignDepositNoahEquivRao"], f"{where}.foreignDepositNoahEquivRao"
        ),
        no_fault=obj["noFault"],
    )


def _parse_miner(obj, eval_day: int, index: int) -> MinerInput:
    where = f"miners[{index}]"
    if not isinstance(obj, dict):
        raise FixtureError(f"{where}: expected object")
    _require_keys(
        obj,
        {
            "coldkey",
            "uid",
            "holdRao",
            "firstActivityDay",
            "entryDepositRao",
            "registrationDay",
            "days",
        },
        where,
    )
    coldkey = obj["coldkey"]
    if not isinstance(coldkey, str) or not coldkey:
        raise FixtureError(f"{where}.coldkey: must be a non-empty string")
    uid = obj["uid"]
    if uid is not None:
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise FixtureError(f"{where}.uid: must be null or a non-negative integer")
    first_activity_day = _int_field(obj["firstActivityDay"], f"{where}.firstActivityDay")
    if first_activity_day > eval_day:
        # A firstActivityDay beyond evalDay is an explicit validation
        # error, not an empty day range.
        raise FixtureError(
            f"{where}.firstActivityDay: {first_activity_day} beyond evalDay {eval_day}"
        )
    days_obj = obj["days"]
    if not isinstance(days_obj, dict):
        raise FixtureError(f"{where}.days: expected object")
    days: dict[int, DayEntry] = {}
    for key, entry in days_obj.items():
        # fullmatch(), not match(): see _int_field's docstring above -- a
        # day key like "1\n" would otherwise pass as canonical, convert to
        # day 1 via int(), and silently overwrite (or be overwritten by)
        # whichever of "1"/"1\n" this dict visits second, losing one
        # day's real content with no error.
        if not isinstance(key, str) or not _DECIMAL.fullmatch(key):
            raise FixtureError(f"{where}.days: bad day key {key!r}")
        d = int(key)
        if not (first_activity_day <= d <= eval_day):
            # No day outside firstActivityDay..evalDay.
            raise FixtureError(
                f"{where}.days[{key}]: outside firstActivityDay..evalDay "
                f"({first_activity_day}..{eval_day})"
            )
        days[d] = _parse_day(entry, f"{where}.days[{key}]")
    for d in range(first_activity_day, eval_day + 1):
        if d not in days:
            # An absent day is a validation error, never an implied miss
            # and never an implied neutral day.
            raise FixtureError(f"{where}.days: day {d} absent")
    cum_locked = cum_unlocked = 0
    for d in range(first_activity_day, eval_day + 1):
        cum_locked += days[d].paid_locked_arrival_rao
        cum_unlocked += days[d].pay_unlocked_rao
        if cum_unlocked > cum_locked:
            # Cumulative unlocks may never exceed cumulative locked
            # arrivals through any day. A token cannot unlock before it
            # arrived locked.
            raise FixtureError(
                f"{where}.days[{d}]: cumulative payUnlockedRao {cum_unlocked} "
                f"exceeds cumulative paidLockedArrivalRao {cum_locked}"
            )
    entry_deposit_rao = _int_field(obj["entryDepositRao"], f"{where}.entryDepositRao")
    registration_day = _int_field(obj["registrationDay"], f"{where}.registrationDay")
    if entry_deposit_rao > 0 and not (first_activity_day <= registration_day <= eval_day):
        # A registrationDay outside firstActivityDay..evalDay with a
        # nonzero entry deposit is a validation error, never silent.
        raise FixtureError(
            f"{where}.registrationDay: {registration_day} outside "
            f"firstActivityDay..evalDay ({first_activity_day}..{eval_day}) "
            f"with nonzero entryDepositRao"
        )
    return MinerInput(
        coldkey=coldkey,
        uid=uid,
        hold_rao=_int_field(obj["holdRao"], f"{where}.holdRao"),
        first_activity_day=first_activity_day,
        entry_deposit_rao=entry_deposit_rao,
        registration_day=registration_day,
        days=days,
    )


def parse_fixture(data) -> Fixture:
    """Parse and strictly validate one ``noah-fixture/6`` object."""
    if not isinstance(data, dict):
        raise FixtureError("fixture: expected a JSON object")
    _require_keys(data, {"schema", "name", "dayEpochTs", "evalDay", "dials", "miners"}, "fixture")
    if data["schema"] != SCHEMA_INPUT:
        raise FixtureError(f"fixture.schema: expected {SCHEMA_INPUT!r}, got {data['schema']!r}")
    name = data["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise FixtureError(f"fixture.name: must match {_NAME.pattern}: {name!r}")
    eval_day = _int_field(data["evalDay"], "fixture.evalDay")
    dials = _parse_dials(data["dials"])
    miners_obj = data["miners"]
    if not isinstance(miners_obj, list):
        raise FixtureError("fixture.miners: expected array")
    miners = tuple(_parse_miner(m, eval_day, i) for i, m in enumerate(miners_obj))
    uids = [m.uid for m in miners if m.uid is not None]
    if len(set(uids)) != len(uids):
        raise FixtureError("fixture.miners: duplicate uid")
    return Fixture(
        name=name,
        day_epoch_ts=_int_field(data["dayEpochTs"], "fixture.dayEpochTs"),
        eval_day=eval_day,
        dials=dials,
        miners=miners,
    )


def load_fixture(path: str | Path) -> Fixture:
    """Load and validate one fixture file."""
    with open(path, "r", encoding="utf-8") as f:
        return parse_fixture(json.load(f))


def render_output(output: dict) -> str:
    """Serialize a ``noah-score-output/6`` object in the canonical form the
    differential compares: sorted keys, two-space indent, trailing newline,
    and no other whitespace variance."""
    return json.dumps(output, sort_keys=True, indent=2) + "\n"
