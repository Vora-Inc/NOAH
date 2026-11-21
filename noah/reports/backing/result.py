"""Canonical result JSON. Integers are decimal strings. No floats."""

from __future__ import annotations

import json
from typing import Any

from .constants import ARK_LABEL, ARK_NOTE, PERCENT_DIVISOR, RAO_PER_ALPHA, SCHEMA_ID, USER_LEG_NOTE


def percent_string(drop_ppm: int) -> str:
    sign = "-" if drop_ppm < 0 else ""
    magnitude = abs(drop_ppm)
    whole, frac = divmod(magnitude, PERCENT_DIVISOR)
    return f"{sign}{whole}.{frac:04d}"


def alpha_decimal(rao: int) -> str:
    sign = "-" if rao < 0 else ""
    magnitude = abs(rao)
    whole, rem = divmod(magnitude, RAO_PER_ALPHA)
    frac = rem // 1_000_000
    return f"{sign}{whole}.{frac:03d}"


def stringify(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("floats are not allowed in a result")
    if isinstance(value, dict):
        return {str(key): stringify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [stringify(item) for item in value]
    raise TypeError(f"unsupported result value {type(value).__name__}")


def canonical_dumps(value: Any) -> str:
    return json.dumps(stringify(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def definitions() -> dict[str, str]:
    return {
        "ark_note": ARK_NOTE,
        "price": "user sale leg P(x + alpha_amount, y - tao_amount)",
        "sale": USER_LEG_NOTE,
        "schema": SCHEMA_ID,
        "units": "rao integers; prices are raw U64F64 bits",
    }


def ark_context(present: bool, total: int, locked: int, available: int) -> dict[str, Any]:
    return {
        "available_rao": available,
        "label": ARK_LABEL,
        "locked_rao": locked,
        "note": ARK_NOTE,
        "present": present,
        "total_rao": total,
    }


def assert_no_floats(value: Any) -> None:
    if isinstance(value, float):
        raise AssertionError("float in result")
    if isinstance(value, dict):
        for item in value.values():
            assert_no_floats(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_floats(item)
