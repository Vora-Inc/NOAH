"""The validator shape may read a stake parked on the designated Ark Validator hotkey
before that hotkey is registered on the subnet (issue #364). The miner shape and the
shared default read keep requiring a registered position."""

import pytest

from noah.deposits.assistant import ledger as L
from noah.deposits.assistant import session as S
from noah.deposits.chain import ceiling_from_chain
from noah.deposits.reads import PositionAnswerUnreadable, PositionNotFound

from .fakes import COLD, NETUID, VAL, FakeSource, N, config
from .test_session import Rig


def unseated(stake):
    return {"stake": stake, "locked": 0, "is_registered": False}


def _ceiling(cfg, source):
    services = S.Services(
        connection=None, source=source, evidence_provider=None, ledger=None
    )
    return S._read_ceiling(cfg, services)


def test_validator_shape_unregistered_with_stake_gets_a_ceiling_from_that_stake():
    src = FakeSource(stake_info_record=unseated(500 * N), available=800 * N)
    ceiling = _ceiling(config(stake_position="validator"), src)
    assert ceiling.amount == 500 * N
    assert src.stake_info_hotkeys == [VAL]


def test_validator_shape_unregistered_zero_stake_is_ceiling_zero_with_plain_reason(tmp_path):
    src = FakeSource(stake_info_record=unseated(0))
    assert _ceiling(config(stake_position="validator"), src).amount == 0
    r = Rig(tmp_path, source=src, stake_position="validator")
    res = r.run()
    assert res["status"] == "refused" and res["code"] == "insufficient_funds"
    assert "Nothing is staked on the designated Ark Validator" in res["reason"]
    assert r.chain.submitted == []


def test_validator_shape_unregistered_with_stake_runs_end_to_end(tmp_path):
    src = FakeSource(stake_info_record=unseated(1000 * N))
    r = Rig(tmp_path, source=src, stake_position="validator")
    assert r.run()["status"] == L.FINALIZED


def test_validator_shape_registered_is_unchanged():
    src = FakeSource(stake=700 * N)
    assert _ceiling(config(stake_position="validator"), src).amount == 700 * N


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        {"stake": 5, "locked": 0},  # no is_registered
        {"stake": 5, "locked": 0, "is_registered": None},
        {"stake": 5, "locked": 0, "is_registered": 0},
        {"locked": 0, "is_registered": False},  # no stake
        {"stake": "5", "locked": 0, "is_registered": False},  # undecoded
        {"stake": -1, "locked": 0, "is_registered": False},
        {"stake": True, "locked": 0, "is_registered": False},
    ],
)
def test_validator_shape_malformed_answer_is_still_refused_as_unreadable(bad):
    src = FakeSource(stake_info_record=bad)
    with pytest.raises(PositionAnswerUnreadable):
        _ceiling(config(stake_position="validator"), src)


def test_miner_shape_unregistered_hotkey_still_refuses_as_before():
    with pytest.raises(PositionNotFound):
        _ceiling(config(), FakeSource(stake_info_record=unseated(0)))
    with pytest.raises(PositionAnswerUnreadable):
        _ceiling(config(), FakeSource(stake_info_record=unseated(5 * N)))


def test_shared_default_path_still_raises_for_the_old_inputs():
    src = FakeSource(stake_info_record=unseated(0))
    with pytest.raises(PositionNotFound):
        ceiling_from_chain(src, coldkey=COLD, hotkey=VAL, netuid=NETUID)
    src = FakeSource(stake_info_record=unseated(5 * N))
    with pytest.raises(PositionAnswerUnreadable):
        ceiling_from_chain(src, coldkey=COLD, hotkey=VAL, netuid=NETUID)
    # an explicit False is the same as the default
    with pytest.raises(PositionNotFound):
        ceiling_from_chain(
            FakeSource(stake_info_record=unseated(0)),
            coldkey=COLD,
            hotkey=VAL,
            netuid=NETUID,
            allow_unregistered_position=False,
        )
