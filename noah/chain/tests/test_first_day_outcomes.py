"""Three first-day outcomes, pinned end to end with nothing in the decision mocked.

Path under test: real Store -> ``runner._sample_holdings`` (via ``run_day``) ->
``build_fixture`` -> ``parse_fixture`` -> ``score_fixture`` -> ``keeper.decide``
-> ``vector.publishable_vector`` / ``period.submission_vector``. Only the chain
(a stub) and the whole-history gate (switched off, as in test_fixture_build.py,
because these stores are one day long on purpose) are stand-ins.

The dials are the shipped profile named by ``NOAH_PARAMETERS_JSON``
(m1-revised-v1), not copies, so a profile edit that changes a first-day result
shows up here.

1. A day with no wallet at all is a normal, unpaid day: sampled with zero rows,
   every live seat at 0, the keeper keeps the subnet alive. It is not a refusal
   and it does not burn to the owner.
2. A newcomer with a recorded entry deposit is credited that deposit, starts at
   streak 1.0 and multiplier 1.0, and takes the whole vector.
3. If that newcomer's seat is owner-controlled, publishing refuses, loudly.
"""

import json
import os

import pytest

from noah.chain import fixture_build as fb
from noah.chain.tests import _fixture_helpers as T
from noah.chain.tests import test_runner_pay as R
from noah.core.keeper import KeeperWouldPayOwner, decide
from noah.core.period import submission_vector
from noah.core.vector import OwnerWouldBePaid, publishable_vector
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture

DEPOSIT = 10_000_000_000
FP_ONE = 1_000_000_000
SEAT = 140
OTHER = 141
OWNER_SEAT = 141
LIVE = [SEAT, OTHER]
HOTKEYS = {SEAT: "A", OTHER: "B"}

_DIAL_NAMES = (
    "targetFp",
    "harshness",
    "capFp",
    "halfLifeDays",
    "streakCeilingFp",
    "streakRampDays",
    "ongoingHoldRao",
)


def _dials():
    path = os.environ.get("NOAH_PARAMETERS_JSON")
    assert path, "NOAH_PARAMETERS_JSON must name the profile (m1-revised-v1)"
    with open(path) as f:
        profile = json.load(f)
    return {k: profile[k] for k in _DIAL_NAMES}


@pytest.fixture(autouse=True)
def _no_history_gate(monkeypatch):
    monkeypatch.setattr("noah.chain.fixture_build.require_whole_history", lambda *a, **k: None)


def _build(store, **kw):
    return fb.build_fixture(
        store,
        name="first-day",
        eval_day=0,
        day_epoch_ts=T.ANCHOR,
        dials=_dials(),
        seats={T.MINER: {T.HOTKEY: SEAT}},
        registration_days={T.MINER: {T.HOTKEY: 0}},
        **kw,
    )


def _weights(scored):
    return {int(k): v for k, v in scored["weightsU16"].items()}


class _Chain:
    """Just enough of the period chain for submission_vector."""

    def __init__(self, owners):
        self._owners = owners

    def live_uids(self, height):
        return LIVE

    def owner_uids(self):
        return self._owners

    def fixture_hotkeys(self, height):
        return HOTKEYS

    def chain_hotkeys(self):
        return HOTKEYS


def _newcomer_store(tmp_path):
    store = T.a_store(tmp_path)
    store.mark_day_scanned(0, from_block=1, to_block=2)
    T.deposited(store, T.MINER, 0, DEPOSIT, origin=T.HOTKEY, hotkey=T.HOTKEY)
    T.held(store, T.MINER, 0, amount=DEPOSIT)
    T.holdings_sampled(store, 0)
    return store


def _newcomer_weights(tmp_path):
    store = _newcomer_store(tmp_path)
    scored = score_fixture(parse_fixture(_build(store, entry_deposits={T.MINER: {T.HOTKEY: DEPOSIT}})))
    return scored


def test_zero_wallet_day_is_sampled_empty_and_keeps_every_seat_at_zero(tmp_path):
    store = R.a_store(tmp_path)
    R.run(store, R.Chain(), day=0, stride=2)

    # _sample_holdings asked the question and wrote no rows.
    assert store.wallets_seen() == []
    assert store.sampling_of_day(0) is not None

    fixture = _build(store)
    assert fixture["miners"] == []
    scored = score_fixture(parse_fixture(fixture))
    assert _weights(scored) == {}

    decision = decide(scored=_weights(scored), live_uids=LIVE, owner_uids={OWNER_SEAT})
    assert decision.keeping_alive is True
    assert decision.vector == {SEAT: 0, OTHER: 0}
    assert decision.vector[OWNER_SEAT] == 0


def test_newcomer_day_one_is_credited_the_entry_deposit_and_takes_the_vector(tmp_path):
    scored = _newcomer_weights(tmp_path)

    (miner,) = scored["miners"]
    # The engine emits fixed-point strings; 1_000_000_000 is 1.0.
    assert miner["noahCreditRao"] == str(DEPOSIT)
    assert miner["streakFp"] == str(FP_ONE)
    assert miner["multiplierFp"] == str(FP_ONE)
    assert miner["payWeightFp"] == "100000"
    assert _weights(scored) == {SEAT: 65535}

    decision = decide(scored=_weights(scored), live_uids=LIVE, owner_uids={OWNER_SEAT})
    assert decision.vector[SEAT] == 65535
    assert decision.vector[OWNER_SEAT] == 0
    publishable_vector(
        scored=decision.vector,
        live_uids=LIVE,
        fixture_hotkeys=HOTKEYS,
        chain_hotkeys=HOTKEYS,
        owner_uids={OWNER_SEAT},
    )


def test_owner_controlled_newcomer_seat_refuses_to_publish(tmp_path):
    weights = _weights(_newcomer_weights(tmp_path))
    assert weights == {SEAT: 65535}

    with pytest.raises(KeeperWouldPayOwner) as keeper:
        submission_vector(_Chain({SEAT}), 1, weights)
    assert "Nothing is published" in str(keeper.value)

    with pytest.raises(OwnerWouldBePaid) as vector:
        publishable_vector(
            scored=weights,
            live_uids=LIVE,
            fixture_hotkeys=HOTKEYS,
            chain_hotkeys=HOTKEYS,
            owner_uids={SEAT},
        )
    assert "Nothing is published" in str(vector.value)
