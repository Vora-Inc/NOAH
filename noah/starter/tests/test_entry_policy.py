# ruff: noqa: F811
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.core.tests._adapter_helpers import FLOORED_ANCHOR
from noah.research.tests.test_entry_migration import (
    REG,
    SEAT0_COLD,
    SEAT0_HOT,
    R,
    snapshot,
    store,  # noqa: F401
)
from noah.starter.entry import EntryPolicy, EntryPolicyRefused


def adapter():
    chain = SimpleNamespace(
        genesis_hash=lambda: "test-genesis",
        block_hash=lambda b: "test-finalized",
        block_timestamp=lambda b: FLOORED_ANCHOR + (86400 + 86399 if b == 101 else 2 * 86400),
        seat_hotkeys=lambda b: {0: SEAT0_HOT},
        hotkey_owners=lambda keys, b: {SEAT0_HOT: SEAT0_COLD},
    )
    return SimpleNamespace(
        runner_chain=chain, entry_deposit_rao=10 * R, netuid=82, day_anchor_ts=FLOORED_ANCHOR
    )


def test_real_receipts_survive_adapter_policy_transition(store):
    a = adapter()
    policy = EntryPolicy(snapshot(store))
    with patch("noah.core.chain_adapter._registration_days_for", return_value=REG):
        assert policy.inputs(a, store, {SEAT0_COLD: {SEAT0_HOT: 0}}, 1, 102) is None
        view, registered, credited, days = policy.inputs(
            a, store, {SEAT0_COLD: {SEAT0_HOT: 0}}, 2, 102
        )
    assert credited == {SEAT0_COLD: {SEAT0_HOT: 10 * R}}
    assert days == {SEAT0_COLD: {SEAT0_HOT: 1}}
    assert view.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, 2) == 20 * R


@pytest.mark.parametrize(
    "change,match",
    [
        ("hash", "finalized block"),
        ("day", "cutoff day"),
        ("seats", "every identity"),
        ("threshold", "ten NOAH"),
    ],
)
def test_migration_cannot_be_reused_on_different_chain_facts(store, change, match):
    a = adapter()
    if change == "hash":
        a.runner_chain.block_hash = lambda b: "other"
    if change == "day":
        a.runner_chain.block_timestamp = lambda b: FLOORED_ANCHOR
    if change == "seats":
        a.runner_chain.seat_hotkeys = lambda b: {}
    if change == "threshold":
        a.entry_deposit_rao = 0
    with pytest.raises(EntryPolicyRefused, match=match):
        EntryPolicy(snapshot(store)).inputs(a, store, {}, 2, 102)


def test_midday_migration_cutoff_refused(store):
    a = adapter()
    a.runner_chain.block_timestamp = lambda b: FLOORED_ANCHOR + 86400 + 100
    with pytest.raises(EntryPolicyRefused, match="last block"):
        EntryPolicy(snapshot(store)).inputs(a, store, {}, 2, 102)


def test_snapshot_tampering_refused_before_connections(store):
    frozen = snapshot(store)
    frozen["payload"]["asOfDay"] = 0
    with pytest.raises(EntryPolicyRefused, match="digest"):
        EntryPolicy(frozen)
