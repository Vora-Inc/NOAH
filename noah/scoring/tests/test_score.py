"""Direct unit tests for ``noah.scoring.score.gate`` -- specifically, that
the ongoing-holding floor already works as "configurable to zero/disabled"
with no code change (Drew's own words, relayed 2026-09-19: "build the floor
as a configurable value that can be set to zero/disabled, not deleted").

``gate(hold_rao, ongoing_hold_rao)`` is ``1 if hold_rao >= ongoing_hold_rao
else 0``. Since a real chain holding is never negative, ``ongoing_hold_rao
== 0`` makes ``hold_rao >= 0`` true for every real holding, including a
literal zero holding -- the gate is unconditionally open, i.e. "disabled".
Nothing in ``gate`` itself, or in ``noah.scoring.fixture_io``'s parsing of
the ``ongoingHoldRao`` dial (see ``test_engine.py::
test_ongoing_hold_rao_of_zero_is_accepted_by_the_fixture_parser``), rejects
a zero value -- there is no positive-only constraint anywhere on this path.
"""

import pytest

from noah.scoring.score import gate

REAL_HOLDINGS_RAO = (
    0,
    1,
    999,
    400_000_000_000 - 1,  # just under the historical 400-NOAH floor
    400_000_000_000,  # exactly the historical 400-NOAH floor
    400_000_000_000 + 1,
    10**18,  # an enormous real holding
)


@pytest.mark.parametrize("hold_rao", REAL_HOLDINGS_RAO)
def test_gate_with_ongoing_hold_rao_zero_always_opens(hold_rao):
    """The floor set to 0 (disabled) always opens the door, for every real
    (nonnegative) holding, including a literal zero holding."""
    assert gate(hold_rao, 0) == 1


def test_gate_with_a_real_positive_floor_still_closes_when_short():
    """Sanity check the parametrized zero-floor claim actually means
    something: the SAME gate function still closes normally once the
    floor is nonzero, so the zero case above is really "disabled", not a
    bug that always returns 1 regardless of the floor."""
    floor = 400_000_000_000
    assert gate(floor - 1, floor) == 0
    assert gate(floor, floor) == 1
    assert gate(floor + 1, floor) == 1
