"""Independent adapter wiring regressions; synthetic readers, no live RPC."""
from types import SimpleNamespace

from noah.chain.store import Store
from noah.chain.tests._agreeing_helpers import (
    ARK, EPOCH, SEAT0_COLD, SEAT0_HOT, SEAT1_COLD, SEAT1_HOT,
    VALIDATOR_HOTKEY,
)
from noah.core.tests._adapter_helpers import an_adapter, DIALS, FLOORED_ANCHOR
from noah.points.receipts import reconcile_fixture


def test_actual_adapter_retains_pre_exclusion_owner_and_hotkey_rosters(tmp_path, monkeypatch):
    """Real read/build/filter with empty entry history drops both live seats.

    Only the migration policy interface is a stub: this tests adapter wiring,
    not startup authentication, settlement or publication.
    """
    import noah.deposits.entry_gate as entry_gate
    monkeypatch.setattr(entry_gate, 'entry_deposit_rao', lambda: 100_000_000_000)
    store = Store.create(tmp_path / 'history.db', day_anchor_ts=FLOORED_ANCHOR)
    original = {SEAT0_COLD: {SEAT0_HOT: 0}, SEAT1_COLD: {SEAT1_HOT: 1}}
    adapter, _, _ = an_adapter(
        seats_source=lambda day, history: original,
        holding_stride=1,
        day_window=lambda height: (0, EPOCH - 2, EPOCH + 2),
        ark_coldkey=ARK, designated_validator_hotkey=VALIDATOR_HOTKEY,
        dials=DIALS, day_anchor_ts=FLOORED_ANCHOR, read_collateral=True,
        store_factory=lambda: store, entry_deposit_rao=100_000_000_000,
        submitted_record_dir=tmp_path / 'scores',
    )
    adapter.points_policy = SimpleNamespace(
        amended=True, input_schema='noah-points-fixture/2',
        migration={'cutoff_day': -1},
        foreign_rule=lambda adapter, rule: rule,
        precollect_foreign=lambda receipts: None,
        check_snapshot=lambda adapter, fixture, hotkeys: True,
        fixture_migration=lambda fixture, hotkeys: {'cutoffDay': -1},
    )
    # _read_verified avoids the separate outage state machine, deliberately
    # outside this wiring regression's scope.
    fixture = adapter._read_verified(EPOCH + 2)
    assert len(adapter._dropped_unconfirmed_entry_deposits) == 2
    assert not [row for row in fixture['miners'] if row['uid'] is not None]
    assert fixture['pointsHotkeys'] == {'0': SEAT0_HOT, '1': SEAT1_HOT}
    assert fixture['pointsOwners'] == {'0': SEAT0_COLD, '1': SEAT1_COLD}
    assert fixture['pointsReceipts'] == []
    assert reconcile_fixture(fixture, [])
    store.close()
