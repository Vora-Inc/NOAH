"""Collateral events are scoped to the configured subnet."""
import pytest

from noah.chain.collateral import (
    CollateralEventUnreadable,
    collateral_locked_at_registration,
    collateral_moved_by_hand,
)

FOREIGN_HOTKEY = "foreign-hotkey"


def lock(netuid=7, hotkey=FOREIGN_HOTKEY, event_id="CollateralLocked"):
    return {"module_id": "SubtensorModule", "event_id": event_id,
            "attributes": {"netuid": netuid, "hotkey": hotkey,
                           "locked": 1000, "total_locked": 1000}}


def registration(netuid=7, hotkey=FOREIGN_HOTKEY):
    return {"module_id": "SubtensorModule", "event_id": "NeuronRegistered",
            "attributes": [netuid, 5, hotkey]}


def test_foreign_subnet_events_are_ignored():
    events = [lock(), registration()]
    assert collateral_moved_by_hand(events, netuid=82) == set()
    assert collateral_locked_at_registration(events, netuid=82) == set()
    assert collateral_moved_by_hand(events, netuid=7) == {FOREIGN_HOTKEY}
    assert collateral_locked_at_registration(events, netuid=7) == {FOREIGN_HOTKEY}


def test_mixed_subnets_and_same_hotkey_do_not_cross_pair():
    for events in ([lock(7), registration(82)], [lock(82), registration(7)]):
        assert collateral_locked_at_registration(events, netuid=82) == set()
    events = [lock(), registration(), lock(82, "local"), registration(82, "local")]
    assert collateral_moved_by_hand(events, netuid=82) == {"local"}
    assert collateral_locked_at_registration(events, netuid=82) == {"local"}


def test_min_collateral_set_uses_runtime_named_netuid():
    local = lock(82, "local", "MinCollateralSet")
    local["attributes"] = {"netuid": 82, "hotkey": "local", "min_locked": 10}
    assert collateral_moved_by_hand([local, lock(7, event_id="MinCollateralSet")],
                                    netuid=82) == {"local"}


@pytest.mark.parametrize("netuid", [None, True, "82", -1, 65536])
@pytest.mark.parametrize("event_id", ["CollateralLocked", "MinCollateralSet"])
def test_unknown_subnet_is_a_refusal(netuid, event_id):
    with pytest.raises(CollateralEventUnreadable):
        collateral_moved_by_hand([lock(netuid, event_id=event_id)], netuid=82)


def test_missing_fields_and_unsupported_local_shapes_refuse():
    for attributes in ({"hotkey": "local"}, [82, "local"], None):
        event = lock(82)
        event["attributes"] = attributes
        with pytest.raises(CollateralEventUnreadable):
            collateral_moved_by_hand([event], netuid=82)
    for attributes in ([82], [82, 1, None], {"netuid": 82, "hotkey": "local"}):
        event = registration(82)
        event["attributes"] = attributes
        with pytest.raises(CollateralEventUnreadable):
            collateral_locked_at_registration([event, lock(82)], netuid=82)


@pytest.mark.parametrize("hotkey", [None, "", [], {}])
def test_malformed_local_lock_hotkey_refuses(hotkey):
    with pytest.raises(CollateralEventUnreadable):
        collateral_moved_by_hand([lock(82, hotkey)], netuid=82)
    with pytest.raises(CollateralEventUnreadable):
        collateral_locked_at_registration([registration(82), lock(82, hotkey)], netuid=82)


def test_foreign_pallet_ignored_missing_pallet_refuses():
    events = [lock(82), registration(82)]
    for event in events:
        event["module_id"] = "AnotherPallet"
    assert collateral_moved_by_hand(events, netuid=82) == set()
    assert collateral_locked_at_registration(events, netuid=82) == set()
    del events[0]["module_id"]
    with pytest.raises(CollateralEventUnreadable):
        collateral_moved_by_hand(events, netuid=82)


def test_paid_wallets_are_read_after_foreign_registration(tmp_path):
    from .test_runner_pay import Chain, EPOCH, NETUID, a_store, pay_event, run

    hotkeys = {uid: f"hotkey-{uid}" for uid in range(3)}
    owners = {hotkey: f"wallet-{uid}" for uid, hotkey in hotkeys.items()}
    chain = Chain(events={EPOCH: [pay_event([100] * 3), lock(), registration()]},
                  seats=hotkeys, owners=owners, registrations={u: 100 for u in hotkeys}, n=3)
    store = a_store(tmp_path)
    run(store, chain)
    assert NETUID != 7
    for coldkey in owners.values():
        assert store.attributed_pay(coldkey, 0) == 100
        assert store.locked_pay_unreadable(coldkey, 0) is None
        assert store.locked_pay(coldkey, 0) == (0, 0)
    assert sum(x == ("coldkey_collateral", EPOCH) for x in chain.asked) == 3
    assert sum(x == ("coldkey_collateral", EPOCH - 1) for x in chain.asked) == 3


def test_foreign_ark_receipt_survives_collateral_filter_in_same_scan(tmp_path):
    from .test_runner_pay import (
        ALICE_COLD, BOB_COLD, Chain, EPOCH, _mixed_deposit_and_epoch_events, a_store, run,
    )

    giver, events = _mixed_deposit_and_epoch_events()
    # This asset is foreign to the scored subnet and still belongs in the Ark
    # receipt history. Only collateral accounting may filter by target subnet.
    events[EPOCH][0]["attributes"][4] = 7
    events[EPOCH][1]["attributes"][3:5] = [7, 7]
    events[EPOCH].extend([lock(), registration()])
    store = a_store(tmp_path)
    run(store, Chain(events=events))
    assert store._conn.execute(
        "SELECT coldkey, token_netuid, amount_rao, block FROM foreign_deposit"
    ).fetchall() == [(giver, 7, 469790773712, EPOCH)]
    assert store._conn.execute("SELECT count(*) FROM noah_deposit").fetchone() == (0,)
    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(BOB_COLD, 0) == 300
    assert store.locked_pay_unreadable(ALICE_COLD, 0) is None
    assert store.locked_pay_unreadable(BOB_COLD, 0) is None
