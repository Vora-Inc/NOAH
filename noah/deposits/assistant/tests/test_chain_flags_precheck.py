"""Before any signature, the chain's own switches must allow the transfer (issue #366).

SubtokenEnabled[netuid] and the subnet's TransferToggle are what the transaction itself needs.
They are not registration checks and these tests never involve hotkey registration.
"""

import pytest

from noah.deposits.assistant import ledger as L
from noah.deposits.assistant import session as S

from .fakes import MINER, NETUID, FakeSource, N
from .test_session import Rig

SHAPES = ["miner", "validator"]


def _assert_nothing_signed(r):
    assert r.chain.submitted == [] and r.signer_calls == 0 and r.keypair.signed == 0
    assert r.ledger.for_miner(MINER) == []  # no live intent


@pytest.mark.parametrize("shape", SHAPES)
def test_enabled_and_permitted_proceeds_as_before(tmp_path, shape):
    r = Rig(tmp_path, source=FakeSource(), stake_position=shape)
    assert S.preview(r.config, r.services())["status"] == "ready"
    assert r.run()["status"] == L.FINALIZED


@pytest.mark.parametrize("shape", SHAPES)
def test_subtoken_disabled_refuses_in_preview_and_run_once(tmp_path, shape):
    r = Rig(tmp_path, source=FakeSource(subtoken=False), stake_position=shape)
    out = S.preview(r.config, r.services())
    assert out["status"] == "refused" and out["code"] == "subtoken_disabled"
    assert "SubtokenEnabled" in out["reason"]
    assert "revert the transaction after you sign" in out["reason"]
    assert "network fee" in out["reason"] and "Nothing was signed or sent" in out["reason"]
    res = r.run()
    assert res["status"] == "refused" and res["code"] == "subtoken_disabled"
    assert "SubtokenEnabled" in res["reason"]
    _assert_nothing_signed(r)


@pytest.mark.parametrize("shape", SHAPES)
def test_transfers_not_permitted_refuses_in_preview_and_run_once(tmp_path, shape):
    r = Rig(tmp_path, source=FakeSource(transfer_toggle=False), stake_position=shape)
    out = S.preview(r.config, r.services())
    assert out["status"] == "refused" and out["code"] == "transfers_not_permitted"
    assert "TransferToggle" in out["reason"] and "not permitted" in out["reason"]
    assert "network fee" in out["reason"]
    res = r.run()
    assert res["status"] == "refused" and res["code"] == "transfers_not_permitted"
    _assert_nothing_signed(r)


UNREADABLE = [None, "", "0x01", "true", "True", 1, 0, b"\x01", [True], {"x": 1}]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("bad", UNREADABLE, ids=repr)
def test_unreadable_subtoken_refuses_never_reads_as_enabled(tmp_path, shape, bad):
    r = Rig(tmp_path, source=FakeSource(subtoken=bad), stake_position=shape)
    for res in (S.preview(r.config, r.services()), r.run()):
        assert res["status"] == "refused" and res["code"] == "subtoken_unreadable"
        assert "SubtokenEnabled" in res["reason"]
    _assert_nothing_signed(r)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("bad", UNREADABLE, ids=repr)
def test_unreadable_transfer_toggle_refuses_never_reads_as_permitted(tmp_path, shape, bad):
    r = Rig(tmp_path, source=FakeSource(transfer_toggle=bad), stake_position=shape)
    for res in (S.preview(r.config, r.services()), r.run()):
        assert res["status"] == "refused" and res["code"] == "transfers_unreadable"
        assert "TransferToggle" in res["reason"]
    _assert_nothing_signed(r)


@pytest.mark.parametrize("which", ["subtoken_enabled", "transfer_toggle"])
def test_a_read_that_raises_refuses_as_unreadable(tmp_path, which):
    class Raising(FakeSource):
        def __getattribute__(self, name):
            if name == which:
                raise ConnectionError("node went away")
            return super().__getattribute__(name)

    r = Rig(tmp_path, source=Raising())
    res = r.run()
    assert res["status"] == "refused" and res["code"].endswith("_unreadable")
    _assert_nothing_signed(r)


def test_a_source_without_the_reads_refuses(tmp_path):
    class Bare(FakeSource):
        subtoken_enabled = None

    r = Rig(tmp_path, source=Bare())
    assert r.run()["code"] == "subtoken_unreadable"


def test_checks_do_not_depend_on_hotkey_registration(tmp_path):
    # An unregistered Ark Validator position with fine chain flags is untouched by the checks.
    src = FakeSource(stake_info_record={"stake": 1000 * N, "locked": 0, "is_registered": False})
    r = Rig(tmp_path, source=src, stake_position="validator")
    assert r.run()["status"] == L.FINALIZED
    # and the new messages never mention registration
    for text in (S.SUBTOKEN_DISABLED, S.SUBTOKEN_UNREADABLE, S.TRANSFERS_NOT_PERMITTED):
        assert "registered" not in text.lower() and "registration" not in text.lower()


def test_flags_fine_leaves_existing_refusals_in_place(tmp_path):
    r = Rig(tmp_path, source=FakeSource(stake=1 * N), stake_position="validator")
    res = r.run()
    assert res["code"] == "insufficient_funds"


def test_miner_shape_zero_stake_keeps_its_own_message(tmp_path):
    r = Rig(tmp_path, source=FakeSource(stake=0), stake_position="miner")
    res = r.run()
    assert res["code"] == "insufficient_funds"
    assert res["reason"] == (
        "Nothing can be sent: 0 NOAH is available and 1 NOAH is held back as your reserve."
    )
    assert S.NOTHING_STAKED_ON_VALIDATOR not in res["reason"]
    assert "designated Ark Validator" not in res["reason"]


def test_netuid_passed_to_the_reads(tmp_path):
    seen = []

    class Spy(FakeSource):
        def subtoken_enabled(self, netuid):
            seen.append(("s", netuid))
            return True

        def transfer_toggle(self, netuid):
            seen.append(("t", netuid))
            return True

    Rig(tmp_path, source=Spy()).run()
    assert ("s", NETUID) in seen and ("t", NETUID) in seen


@pytest.mark.parametrize("shape", SHAPES)
def test_transfer_toggle_absent_but_effective_true_does_not_refuse(tmp_path, shape):
    # Measured on the spec 467 test chain, netuid 2: TransferToggle has no stored key, the
    # runtime default (True) applies, and the transfer is permitted. This must not refuse.
    class AbsentToggle(FakeSource):
        def transfer_toggle_raw(self, netuid):  # the raw key is absent; must not be consulted
            return None

    r = Rig(tmp_path, source=AbsentToggle(transfer_toggle=True), stake_position=shape)
    assert S.preview(r.config, r.services())["status"] == "ready"
    assert r.run()["status"] == L.FINALIZED


def test_subtoken_absent_effective_false_refuses_as_disabled(tmp_path):
    # Absent SubtokenEnabled decodes to the runtime default False: the chain would revert.
    r = Rig(tmp_path, source=FakeSource(subtoken=False))
    assert r.run()["code"] == "subtoken_disabled"
    _assert_nothing_signed(r)


@pytest.mark.parametrize(
    "kw, code",
    [
        ({}, None),
        ({"subtoken": False}, "subtoken_disabled"),
        ({"transfer_toggle": False}, "transfers_not_permitted"),
    ],
)
def test_stored_true_and_stored_false_for_each_flag(tmp_path, kw, code):
    res = Rig(tmp_path, source=FakeSource(**kw)).run()
    assert (res.get("code") if code else res["status"]) == (code or L.FINALIZED)
