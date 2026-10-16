"""Positive control for the production deposit scan's two filters.

An empty ``noah_deposit`` table can mean two different things: no deposit
existed in the scanned blocks, or the filter was pointed at the wrong thing and
matched nothing. These tests build a block that DOES hold a deposit into the
production Ark and show the same scan finds it, so a zero in production can be
read against a known-good result.

How to read production results:

* Zero ``noah_deposit`` rows means one of two things: no stake transfer into the
  Ark coldkey occurred in the scanned blocks, or the Ark address given to the
  scan (``--ark-address``) was wrong. A wrong Ark address always looks like
  "nothing happened" (see ``test_a_wrong_ark_address_finds_nothing``), so check
  the address against the production value before concluding anything.
* A designated-validator mismatch only marks a row, it never removes one. A
  deposit staked to another hotkey is still recorded, with an
  ``unattributed_reason`` saying it does not qualify for mining credit. A wrong
  ``--validator-hotkey`` therefore shows up as every row carrying that reason,
  not as missing rows.
* A day with no deposit is still marked scanned: empty by fact is a recorded
  result, not a gap.

All addresses below are the public production values.
"""

import hashlib

from noah.chain.deposits import deposits_in_block
from noah.chain.runner import run_day
from noah.chain.ss58 import public_key_of
from noah.chain.store import Store
from noah.chain.tests._deposit_helpers import FakeOwnerSource
from noah.chain.tests.test_runner import ANCHOR, Chain

# Production values (public addresses).
NOAH_ARK_ADDRESS = "5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3"
NOAH_VALIDATOR_HOTKEY = "5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV"
COMMIT_HOTKEY = "5Hn6PHyjx2rLY3EXCP1xK7XHUquRo17fyuyM1HuZrAWb8y3G"  # a different thing
NETUID = 82

# Other valid addresses used as the depositor, a miner, another validator and
# another coldkey (the values the existing deposit tests already use).
DEPOSITOR = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
MINER_HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
OTHER_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
OTHER_COLDKEY = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"

BLOCK = 8_800_001
PARENT_HASH = "0x" + format(BLOCK - 1, "x")
ALPHA = 469_790_773_712
_MINER_RAW = public_key_of(MINER_HOTKEY)
_MINER_HASH = "0x" + hashlib.blake2b(_MINER_RAW, digest_size=32).hexdigest()

NOT_DESIGNATED = "not the designated Ark Validator"


def test_the_production_strings_are_valid_addresses():
    for address in (NOAH_ARK_ADDRESS, NOAH_VALIDATOR_HOTKEY, COMMIT_HOTKEY):
        assert len(public_key_of(address)) == 32
    assert len({NOAH_ARK_ADDRESS, NOAH_VALIDATOR_HOTKEY, COMMIT_HOTKEY}) == 3


def deposit_events(xt, ordinal, *, destination, hotkey, depositor=DEPOSITOR):
    """A StakeAdded + StakeTransferred pair plus the Remarked event of the batch."""
    base = {"phase": "ApplyExtrinsic", "extrinsic_idx": xt}
    return [
        {
            **base,
            "ordinal_in_block": ordinal,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [destination, hotkey, 8_000_000_000, ALPHA, NETUID, 0],
        },
        {
            **base,
            "ordinal_in_block": ordinal + 1,
            "module_id": "SubtensorModule",
            "event_id": "StakeTransferred",
            "attributes": [depositor, destination, hotkey, NETUID, NETUID, 8_000_000_000],
        },
        {
            **base,
            "ordinal_in_block": ordinal + 2,
            "module_id": "System",
            "event_id": "Remarked",
            "attributes": {"sender": depositor, "hash": _MINER_HASH},
        },
    ]


def batch_all_extrinsic(*, destination, hotkey, depositor=DEPOSITOR):
    return {
        "address": depositor,
        "call": {
            "call_module": "Utility",
            "call_function": "batch_all",
            "call_args": [
                {
                    "name": "calls",
                    "value": [
                        {
                            "call_module": "SubtensorModule",
                            "call_function": "transfer_stake",
                            "call_args": [
                                {"name": "destination_coldkey", "value": destination},
                                {"name": "hotkey", "value": hotkey},
                                {"name": "origin_netuid", "value": NETUID},
                                {"name": "destination_netuid", "value": NETUID},
                                {"name": "alpha_amount", "value": ALPHA},
                            ],
                        },
                        {
                            "call_module": "System",
                            "call_function": "remark_with_event",
                            "call_args": [{"name": "remark", "value": "0x" + _MINER_RAW.hex()}],
                        },
                    ],
                }
            ],
        },
    }


def extrinsics_list(*batches):
    """Extrinsic slots 0..6 are padding, so the batches sit at index 7, 8, ..."""
    padding = [
        {
            "address": None,
            "call": {"call_module": "Timestamp", "call_function": "set", "call_args": []},
        }
        for _ in range(7)
    ]
    return padding + list(batches)


def scan_block(events, extrinsics, *, ark=NOAH_ARK_ADDRESS, validator=NOAH_VALIDATOR_HOTKEY):
    return deposits_in_block(
        events,
        ark_coldkey=ark,
        block=BLOCK,
        block_hash="0xabc",
        designated_validator_hotkey=validator,
        extrinsics=extrinsics,
        parent_block_hash=PARENT_HASH,
        owner_source=FakeOwnerSource({(MINER_HOTKEY, PARENT_HASH): DEPOSITOR}),
    )


def good_deposit():
    """One deposit into the Ark, staked to the designated validator, batch_all + remark."""
    events = deposit_events(7, 145, destination=NOAH_ARK_ADDRESS, hotkey=NOAH_VALIDATOR_HOTKEY)
    extrinsics = extrinsics_list(
        batch_all_extrinsic(destination=NOAH_ARK_ADDRESS, hotkey=NOAH_VALIDATOR_HOTKEY)
    )
    return events, extrinsics


# ---- (1) the filter finds a real deposit -----------------------------------


def test_a_deposit_to_the_designated_validator_is_found_and_attributed():
    events, extrinsics = good_deposit()
    got = scan_block(events, extrinsics)
    assert len(got) == 1
    assert got[0].coldkey == DEPOSITOR
    assert got[0].amount_rao == ALPHA
    assert got[0].hotkey == NOAH_VALIDATOR_HOTKEY
    assert got[0].origin_hotkey == MINER_HOTKEY
    assert got[0].unattributed_reason is None


# ---- (2) a wrong-validator deposit is kept, marked, not dropped -------------


def test_a_deposit_to_another_validator_is_still_a_row_with_the_reason():
    events, extrinsics = good_deposit()
    events += deposit_events(8, 160, destination=NOAH_ARK_ADDRESS, hotkey=OTHER_VALIDATOR)
    extrinsics.append(batch_all_extrinsic(destination=NOAH_ARK_ADDRESS, hotkey=OTHER_VALIDATOR))
    got = scan_block(events, extrinsics)
    assert len(got) == 2  # a filter that matched nothing would give zero for both
    by_hotkey = {row.hotkey: row for row in got}
    assert by_hotkey[NOAH_VALIDATOR_HOTKEY].unattributed_reason is None
    marked = by_hotkey[OTHER_VALIDATOR]
    assert marked.unattributed_reason == (
        f"staked to {OTHER_VALIDATOR}, not the designated Ark Validator "
        f"{NOAH_VALIDATOR_HOTKEY} -- does not qualify for mining credit"
    )
    assert NOT_DESIGNATED in marked.unattributed_reason
    assert marked.amount_rao == ALPHA


def test_a_deposit_to_the_old_replaced_hotkey_is_recorded_but_uncredited():
    old = "5EQhYhR7kcCVztyxpiZmJDJ4W9A1YDEGZX7w9xgCt5xXvTEW"
    events, extrinsics = good_deposit()
    events += deposit_events(8, 160, destination=NOAH_ARK_ADDRESS, hotkey=old)
    extrinsics.append(batch_all_extrinsic(destination=NOAH_ARK_ADDRESS, hotkey=old))
    got = scan_block(events, extrinsics)
    assert len(got) == 2
    by_hotkey = {row.hotkey: row for row in got}
    assert by_hotkey[NOAH_VALIDATOR_HOTKEY].unattributed_reason is None
    assert NOT_DESIGNATED in by_hotkey[old].unattributed_reason
    assert by_hotkey[old].amount_rao == ALPHA


# ---- (3) the Ark-coldkey filter really selects -------------------------------


def test_a_deposit_into_a_different_coldkey_leaves_no_row():
    events = deposit_events(7, 145, destination=OTHER_COLDKEY, hotkey=NOAH_VALIDATOR_HOTKEY)
    extrinsics = extrinsics_list(
        batch_all_extrinsic(destination=OTHER_COLDKEY, hotkey=NOAH_VALIDATOR_HOTKEY)
    )
    assert scan_block(events, extrinsics) == []


def test_only_the_ark_deposit_is_kept_when_both_kinds_share_a_block():
    events, extrinsics = good_deposit()
    events += deposit_events(8, 160, destination=OTHER_COLDKEY, hotkey=NOAH_VALIDATOR_HOTKEY)
    extrinsics.append(batch_all_extrinsic(destination=OTHER_COLDKEY, hotkey=NOAH_VALIDATOR_HOTKEY))
    got = scan_block(events, extrinsics)
    assert [row.event_ordinal for row in got] == [145]


# ---- (4) empty by fact -------------------------------------------------------


def test_a_block_with_no_stake_events_is_empty():
    assert scan_block([], extrinsics_list()) == []


# ---- (6) the failure signatures of a wrong argument --------------------------


def test_a_wrong_ark_address_finds_nothing():
    """The 'filter matches nothing' signature: the same block as test (1), zero rows."""
    events, extrinsics = good_deposit()
    assert len(scan_block(events, extrinsics)) == 1  # the control: it is there
    assert scan_block(events, extrinsics, ark=COMMIT_HOTKEY) == []
    assert scan_block(events, extrinsics, ark=OTHER_COLDKEY) == []


def test_a_wrong_designated_validator_marks_the_row_and_does_not_remove_it():
    events, extrinsics = good_deposit()
    got = scan_block(events, extrinsics, validator=COMMIT_HOTKEY)
    assert len(got) == 1
    assert got[0].unattributed_reason == (
        f"staked to {NOAH_VALIDATOR_HOTKEY}, not the designated Ark Validator "
        f"{COMMIT_HOTKEY} -- does not qualify for mining credit"
    )


# ---- (5) through the real run_day on a real Store ----------------------------


def a_store(tmp_path):
    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    store.record_emission_start(block=8_000_000, block_timestamp=ANCHOR, block_hash="0xs")
    return store


def scan_day(store, chain, *, ark=NOAH_ARK_ADDRESS, validator=NOAH_VALIDATOR_HOTKEY):
    return run_day(
        store,
        chain,
        day=0,
        from_block=8_800_000,
        to_block=8_800_002,
        ark_coldkey=ark,
        designated_validator_hotkey=validator,
        sources={"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        may_resume=True,
        attribute_deposits=True,
    )


def deposit_rows(store):
    return store._conn.execute(
        "SELECT coldkey, hotkey, origin_hotkey, unattributed_reason FROM noah_deposit"
    ).fetchall()


def day_chain(with_deposit):
    if not with_deposit:
        return Chain()
    events, extrinsics = good_deposit()
    return Chain(
        deposits={BLOCK: events},
        extrinsics={BLOCK: extrinsics},
        owners_at_hash={(MINER_HOTKEY, PARENT_HASH): DEPOSITOR},
    )


def test_run_day_stores_the_deposit_and_sees_the_depositor(tmp_path):
    store = a_store(tmp_path)
    scan_day(store, day_chain(with_deposit=True))
    assert deposit_rows(store) == [(DEPOSITOR, NOAH_VALIDATOR_HOTKEY, MINER_HOTKEY, None)]
    assert store.noah_deposits(DEPOSITOR, 0) == ALPHA
    assert DEPOSITOR in store.wallets_seen()
    assert store.day_was_scanned(0)


def test_run_day_with_no_deposit_is_empty_by_fact_and_still_scanned(tmp_path):
    store = a_store(tmp_path)
    scan_day(store, day_chain(with_deposit=False))
    assert deposit_rows(store) == []
    assert store.wallets_seen() == []
    assert store.noah_deposits(DEPOSITOR, 0) == 0
    assert store.day_was_scanned(0)


def test_run_day_with_a_wrong_ark_address_looks_empty_though_the_deposit_exists(tmp_path):
    """Same chain as the positive run; only the Ark address differs. The day is
    marked scanned with zero rows, which is why a zero alone cannot clear the address."""
    store = a_store(tmp_path)
    scan_day(store, day_chain(with_deposit=True), ark=COMMIT_HOTKEY)
    assert deposit_rows(store) == []
    assert DEPOSITOR not in store.wallets_seen()
    assert store.day_was_scanned(0)


def test_run_day_with_a_wrong_validator_keeps_the_row_marked(tmp_path):
    store = a_store(tmp_path)
    scan_day(store, day_chain(with_deposit=True), validator=COMMIT_HOTKEY)
    rows = deposit_rows(store)
    assert len(rows) == 1
    assert NOT_DESIGNATED in rows[0][3]
    assert DEPOSITOR in store.wallets_seen()
    assert store.noah_deposits(DEPOSITOR, 0) == 0  # kept, but does not count toward credit
