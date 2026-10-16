"""Deterministic Noah scoring over validated fixture inputs.

The Python engine is compared with an independent TypeScript implementation.
Agreement establishes consistency between implementations, not the correctness of
unverified chain inputs or approval of the configured economic policy."""

from .engine import MinerResult, score_fixture, score_miner
from .fixture_io import (
    DayEntry,
    Dials,
    Fixture,
    FixtureError,
    MinerInput,
    load_fixture,
    parse_fixture,
    render_output,
)

__all__ = [
    "DayEntry",
    "Dials",
    "Fixture",
    "FixtureError",
    "MinerInput",
    "MinerResult",
    "load_fixture",
    "parse_fixture",
    "render_output",
    "score_fixture",
    "score_miner",
]
