"""Read entry and holding requirements from NOAH_PARAMETERS_JSON.

Each call reloads and validates the profile. Missing or malformed configuration
raises instead of supplying a default. These values are policy inputs and must be
consistent with the profile used by the scorer."""

from __future__ import annotations

from noah.scoring.parameters import ParametersError, load_parameters


def _load():
    try:
        return load_parameters()
    except ParametersError as error:
        raise RuntimeError(str(error)) from error


def entry_deposit_rao() -> int:
    """Read the current profile's entry requirement on every call; never supply a fallback."""
    return _load().entry_deposit_per_hotkey_rao


def ongoing_hold_rao() -> int:
    """Read the current profile's holding floor; an explicitly configured zero is valid."""
    return _load().ongoing_hold_rao
