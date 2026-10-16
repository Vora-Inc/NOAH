"""Load a versioned subnet parameters profile from NOAH_PARAMETERS_JSON.

Validate the profile's identity, field types and bounds. Profiles supply entry,
holding, contribution and decay settings; a filename alone does not establish
that a profile is the intended policy."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..pathtext import plain_path_text  # noqa: F401  (re-exported)

#: The env var this loader reads, named as its own, separate setting rather
#: than reusing ``NOAH_DIALS_JSON``: the two files carry different shapes
#: (this one adds ``entryDepositPerHotkeyRao``, a deposit fact, alongside the
#: seven scoring dials) and are not interchangeable.
ENV_VAR = "NOAH_PARAMETERS_JSON"

_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")

_REQUIRED_KEYS = frozenset(
    {
        "profileVersion",
        "createdAt",
        "targetFp",
        "harshness",
        "capFp",
        "halfLifeDays",
        "streakCeilingFp",
        "streakRampDays",
        "ongoingHoldRao",
        "entryDepositPerHotkeyRao",
    }
)


class ParametersError(ValueError):
    """The parameters file named by ``NOAH_PARAMETERS_JSON`` is missing,
    unreadable, or does not carry a valid profile. Always refuses loudly:
    there is no quiet fallback to a value invented here."""


def _decimal_field(obj: dict, key: str, where: str) -> int:
    """A non-negative integer, written as a plain JSON integer or a
    canonical decimal string -- the identical encoding
    ``noah.scoring.fixture_io._int_field`` already uses for every dial, kept
    independent here (this module imports nothing from ``fixture_io``) so
    this loader has no dependency on that module's own evolution."""
    if key not in obj:
        raise ParametersError(f"{where}: missing field {key!r}")
    value = obj[key]
    if isinstance(value, bool):
        raise ParametersError(f"{where}.{key}: expected integer, got boolean")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        if not _DECIMAL.fullmatch(value):
            raise ParametersError(f"{where}.{key}: not a canonical decimal string: {value!r}")
        n = int(value)
    else:
        raise ParametersError(f"{where}.{key}: expected integer or decimal string")
    if n < 0:
        raise ParametersError(f"{where}.{key}: negative value {n}")
    return n


def _harshness_field(obj: dict, where: str) -> Fraction:
    if "harshness" not in obj:
        raise ParametersError(f"{where}: missing field 'harshness'")
    raw = obj["harshness"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ParametersError(
            f"{where}.harshness: expected an integer, a decimal number, or a decimal "
            f"string, got {raw!r}"
        )
    # str() first, always: Fraction(1.5) would take the exact binary float
    # expansion of 1.5's IEEE-754 representation, not the decimal value 1.5
    # a human wrote in the profile. str(1.5) == "1.5", and Fraction("1.5")
    # reads that decimal exactly. This is never a shortcut for int/str
    # inputs either -- str(2) == "2" and Fraction("2") == Fraction(2, 1),
    # identical to the direct construction, so one code path handles all
    # three input shapes with no special-casing.
    try:
        harshness = Fraction(str(raw))
    except (ValueError, ZeroDivisionError) as failure:
        raise ParametersError(
            f"{where}.harshness: not a valid exact rational: {raw!r} ({failure})"
        ) from failure
    if harshness <= 0:
        raise ParametersError(f"{where}.harshness: must be positive: {harshness}")
    return harshness


def _string_field(obj: dict, key: str, where: str) -> str:
    if key not in obj:
        raise ParametersError(f"{where}: missing field {key!r}")
    value = obj[key]
    if not isinstance(value, str) or not value:
        raise ParametersError(f"{where}.{key}: must be a non-empty string")
    return value


@dataclass(frozen=True)
class Parameters:
    """A validated versioned profile containing the subnet's scoring policy inputs."""

    profile_version: str
    created_at: str
    target_fp: int
    harshness: Fraction
    cap_fp: int
    half_life_days: int
    streak_ceiling_fp: int
    streak_ramp_days: int
    ongoing_hold_rao: int
    entry_deposit_per_hotkey_rao: int


DECISION_MARKER = "DECISION_REQUIRED"


#: The template's own ``profileVersion``. A decided profile must not keep it.
TEMPLATE_PROFILE_VERSION = "mainnet-production-TEMPLATE-undecided"


def _undecided_fields(obj: dict) -> list[tuple[str, str]]:
    """``(field, marker text)`` for every required field still holding a
    :data:`DECISION_MARKER`, in the profile's own field order."""
    return [
        (key, value.strip())
        for key, value in obj.items()
        if key in _REQUIRED_KEYS
        and isinstance(value, str)
        and value.strip().startswith(DECISION_MARKER)
    ]


def parse_parameters(obj, where: str = "parameters") -> Parameters:
    """Parse an already-loaded JSON object into a :class:`Parameters`.

    Strict like ``noah.scoring.fixture_io._parse_dials``: an unknown or a
    missing field is refused outright, never guessed at or ignored.
    """
    if not isinstance(obj, dict):
        raise ParametersError(f"{where}: expected a JSON object")
    unknown = set(obj) - _REQUIRED_KEYS
    if unknown:
        raise ParametersError(f"{where}: unknown field(s) {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(obj)
    if missing:
        raise ParametersError(f"{where}: missing field(s) {sorted(missing)}")
    undecided = _undecided_fields(obj)
    if undecided:
        detail = "; ".join(f"{key} ({marker})" for key, marker in undecided)
        raise ParametersError(
            f"{where}: this is a TEMPLATE, not a decided profile. {len(undecided)} "
            f"setting(s) have no recorded decision yet and are deliberately left as "
            f"markers, never as a guessed number: {detail}. Nothing was loaded. Get each "
            f"decision recorded on the issue named in its marker, then write the decided "
            f"values into a NEW profile file (with its own profileVersion) and point "
            f"{ENV_VAR} at that."
        )
    if obj.get("profileVersion") == TEMPLATE_PROFILE_VERSION:
        raise ParametersError(
            f"{where}: profileVersion is still {TEMPLATE_PROFILE_VERSION!r}, the "
            f"undecided template's own name. A decided profile is a NEW file with its own "
            f"profileVersion; give this one a version of its own before pointing "
            f"{ENV_VAR} at it. Nothing was loaded."
        )

    target_fp = _decimal_field(obj, "targetFp", where)
    if target_fp <= 0:
        raise ParametersError(f"{where}.targetFp: must be positive: {target_fp}")
    half_life_days = _decimal_field(obj, "halfLifeDays", where)
    if half_life_days < 1:
        raise ParametersError(f"{where}.halfLifeDays: must be >= 1")
    streak_ramp_days = _decimal_field(obj, "streakRampDays", where)
    if streak_ramp_days < 1:
        raise ParametersError(f"{where}.streakRampDays: must be >= 1")

    return Parameters(
        profile_version=_string_field(obj, "profileVersion", where),
        created_at=_string_field(obj, "createdAt", where),
        target_fp=target_fp,
        harshness=_harshness_field(obj, where),
        cap_fp=_decimal_field(obj, "capFp", where),
        half_life_days=half_life_days,
        streak_ceiling_fp=_decimal_field(obj, "streakCeilingFp", where),
        streak_ramp_days=streak_ramp_days,
        ongoing_hold_rao=_decimal_field(obj, "ongoingHoldRao", where),
        entry_deposit_per_hotkey_rao=_decimal_field(obj, "entryDepositPerHotkeyRao", where),
    )


@dataclass(frozen=True)
class ParametersSnapshot:
    """Validated parameters and the exact single file read used to parse them."""

    parameters: Parameters
    source_bytes: bytes
    profile: dict


def load_parameters_snapshot(env: dict | None = None) -> ParametersSnapshot:
    """Read and parse the file named by ``NOAH_PARAMETERS_JSON``.

    ``env`` defaults to the real process environment; a test passes its own
    mapping instead of mutating ``os.environ``. Absence of the env var,
    absence or unreadability of the file it names, and a malformed file
    contents are each their own loud, named :class:`ParametersError` --
    there is no default profile and no quiet fallback to an old one.
    """
    mapping = os.environ if env is None else env
    path = mapping.get(ENV_VAR, "").strip()
    if not path:
        raise ParametersError(
            f"{ENV_VAR} is not set, and there is no default: this loader never guesses at "
            f"the subnet's real parameters. Point it at a real, settled profile file, e.g. "
            f"docs/parameters/profiles/m1-revised-v1.json (repository root). Nothing was "
            f"loaded."
        )
    shown = plain_path_text(path)
    try:
        source_bytes = Path(path).read_bytes()
        raw = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        # The path is operator-typed and the OSError text repeats it: say the kind of failure only.
        raise ParametersError(
            f"{ENV_VAR} names {shown}, and it could not be read as a parameters JSON file "
            f"({type(failure).__name__}). Nothing was loaded."
        ) from None
    try:
        parameters = parse_parameters(raw, where=f"{ENV_VAR} ({shown})")
        return ParametersSnapshot(parameters, source_bytes, raw)
    except ParametersError as failure:
        # Re-raised with the same message; the wrapping here only documents,
        # at this one call site, that a malformed FILE (as opposed to a
        # malformed already-loaded object passed directly to
        # parse_parameters) reaches this exact refusal path too.
        raise failure


def load_parameters(env: dict | None = None) -> Parameters:
    """Load the canonical validated profile without retaining its source bytes."""
    return load_parameters_snapshot(env).parameters
