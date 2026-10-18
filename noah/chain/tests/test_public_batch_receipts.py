"""Public batch-receipt regressions: entirely synthetic decoded calls and events.

No captured transactions, private operator identities, RPCs or signing keys.
These tests exercise receipt boundaries; they do not prove SDK decoding or live
execution. Repeated-byte public identities below are test data only."""

import copy
import hashlib
from dataclasses import asdict
from unittest.mock import patch

import pytest

from noah.chain.deposits import NotADepositEvent, deposits_in_block
from noah.chain.ss58 import address_of
from noah.chain.store import ObservationChanged, Store
from noah.deposits.entry_gate import EntryDepositNotConfirmed, confirm_entry_before_score

ARK, VALIDATOR, COLD, SIGNER, OTHER = [address_of(bytes([i]) * 32) for i in range(1, 6)]
AMOUNT = 10_000_000_000
MINERS = [address_of(bytes([i]) * 32) for i in range(8, 40)]


class Owner:
    def __init__(self, wrong=None, unreadable=None):
        self.asked = []
        self.wrong, self.unreadable = wrong, unreadable

    def owner_at(self, *, hotkey, block_hash):
        self.asked.append((hotkey, block_hash))
        if hotkey == self.unreadable:
            raise RuntimeError("unavailable historical owner")
        return OTHER if hotkey == self.wrong else COLD


def call(module, name, **kwargs):
    return dict(
        call_module=module,
        call_function=name,
        call_args=[dict(name=k, value=v) for k, v in kwargs.items()],
    )


def fixture(count=25, multisig=True, amounts=None):
    events, legs = [], []

    def event(module, name, attributes):
        events.append(
            dict(
                module_id=module,
                event_id=name,
                attributes=attributes,
                ordinal_in_block=len(events) + 3,
                extrinsic_idx=2,
            )
        )

    for i in range(count):
        amount = AMOUNT if amounts is None else amounts[i]
        if amount == 0:
            continue  # The manual sender omits zero rows, not zero-value transfers.
        raw = bytes([i + 8]) * 32
        legs += [
            call(
                "SubtensorModule",
                "transfer_stake",
                destination_coldkey=ARK,
                hotkey=VALIDATOR,
                origin_netuid=82,
                destination_netuid=82,
                alpha_amount=amount,
            ),
            call("System", "remark_with_event", remark="0x" + raw.hex()),
        ]
        event("SubtensorModule", "StakeRemoved", [COLD, VALIDATOR, 123, amount, 82, 0])
        event("SubtensorModule", "StakeAdded", [ARK, VALIDATOR, 123, amount, 82, 0])
        event("SubtensorModule", "StakeTransferred", [COLD, ARK, VALIDATOR, 82, 82, 123])
        event("Utility", "ItemCompleted", [])
        event(
            "System",
            "Remarked",
            {"sender": COLD, "hash": "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()},
        )
        event("Utility", "ItemCompleted", [])
    event("Utility", "BatchCompleted", [])
    inner = call("Utility", "batch_all", calls=legs)
    if multisig:
        event("Multisig", "MultisigExecuted", {"multisig": COLD, "result": {"Ok": []}})
        inner = call("Multisig", "as_multi", call=inner)
    event("System", "ExtrinsicSuccess", {})
    return events, [{}, {}, {"address": SIGNER if multisig else COLD, "call": inner}]


def read(events, extrinsics, owner=None, validator=VALIDATOR):
    return deposits_in_block(
        events,
        ARK,
        100,
        "0xabc",
        validator,
        extrinsics=extrinsics,
        parent_block_hash="0xparent",
        owner_source=owner or Owner(),
    )


def inner_calls(extrinsics):
    c = extrinsics[2]["call"]
    if c["call_module"] == "Multisig":
        c = c["call_args"][0]["value"]
    return c["call_args"][0]["value"]


@pytest.mark.parametrize(
    "count,multisig", [(32, True), (25, True), (25, False), (1, True), (18, False), (8, False)]
)
def test_complete_batches_keep_each_miner_and_actual_receipt(count, multisig):
    events, xts = fixture(count, multisig)
    owner = Owner()
    rows = read(events, xts, owner)
    assert len(rows) == count
    assert sum(d.amount_rao for d in rows) == count * AMOUNT
    assert [d.origin_hotkey for d in rows] == MINERS[:count]
    assert len({(d.block, d.event_ordinal) for d in rows}) == count
    assert all(
        d.extrinsic_index == 2
        and d.coldkey == COLD
        and d.owner_at_deposit == COLD
        and d.hotkey == VALIDATOR
        and d.token_netuid == 82
        and d.unattributed_reason is None
        for d in rows
    )
    assert owner.asked == [(m, "0xparent") for m in MINERS[:count]]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_boundary",
        "missing_completion",
        "outer_failure",
        "inner_failure",
        "missing_multisig_execution",
        "wrong_multisig",
        "wrong_sender",
        "wrong_hash",
        "short_remark",
        "wrong_amount",
        "wrong_destination",
        "wrong_netuid",
        "wrong_transfer_source",
        "wrong_transfer_position",
        "reordered_remarks",
        "extra_call",
        "nested_batch",
        "batch_not_atomic",
        "proxy_wrapper",
        "duplicate_argument",
        "duplicate_ordinal",
        "extra_arrival",
    ],
)
def test_incomplete_or_mismatched_batches_never_assign_credit(mutation):
    events, xts = fixture()
    legs = inner_calls(xts)
    if mutation == "missing_boundary":
        del events[3]
    elif mutation == "missing_completion":
        events[:] = [e for e in events if e["event_id"] != "BatchCompleted"]
    elif mutation == "outer_failure":
        events[-1]["event_id"] = "ExtrinsicFailed"
    elif mutation == "inner_failure":
        events[-2]["attributes"]["result"] = {"Err": "failure"}
    elif mutation == "missing_multisig_execution":
        del events[-2]
    elif mutation == "wrong_multisig":
        events[-2]["attributes"]["multisig"] = OTHER
    elif mutation == "wrong_sender":
        events[4]["attributes"]["sender"] = OTHER
    elif mutation == "wrong_hash":
        events[4]["attributes"]["hash"] = "0x" + "00" * 32
    elif mutation == "short_remark":
        legs[1]["call_args"][0]["value"] = "0x01"
    elif mutation in ("wrong_amount", "wrong_destination", "wrong_netuid"):
        name, value = {
            "wrong_amount": ("alpha_amount", AMOUNT + 1),
            "wrong_destination": ("destination_coldkey", OTHER),
            "wrong_netuid": ("origin_netuid", 0),
        }[mutation]
        for a in legs[0]["call_args"]:
            if a["name"] == name:
                a["value"] = value
    elif mutation == "wrong_transfer_source":
        events[2]["attributes"][0] = OTHER
    elif mutation == "wrong_transfer_position":
        events[2]["attributes"][2] = OTHER
    elif mutation == "reordered_remarks":
        legs[1], legs[3] = legs[3], legs[1]
    elif mutation == "extra_call":
        legs.append(call("System", "remark", remark="0x01"))
    elif mutation == "nested_batch":
        legs[0] = call("Utility", "batch_all", calls=[legs[0]])
    elif mutation == "batch_not_atomic":
        xts[2]["call"]["call_args"][0]["value"]["call_function"] = "batch"
    elif mutation == "proxy_wrapper":
        xts[2]["call"] = call("Proxy", "proxy", real=COLD, call=xts[2]["call"])
    elif mutation == "duplicate_argument":
        legs[0]["call_args"].append(copy.deepcopy(legs[0]["call_args"][0]))
    elif mutation == "duplicate_ordinal":
        events[4]["ordinal_in_block"] = events[3]["ordinal_in_block"]
    elif mutation == "extra_arrival":
        extra = copy.deepcopy(events[1])
        extra["ordinal_in_block"] = 1000
        events.append(extra)
    try:
        rows = read(events, xts)
    except NotADepositEvent:
        return  # Existing structural-refusal behavior also grants no credit.
    assert rows and all(d.unattributed_reason is not None for d in rows)
    assert all(d.origin_hotkey is None for d in rows)


def test_historical_ownership_failure_affects_only_its_miner():
    rows = read(*fixture(), owner=Owner(wrong=MINERS[3], unreadable=MINERS[7]))
    assert len(rows) == 25
    assert [i for i, d in enumerate(rows) if d.unattributed_reason] == [3, 7]
    assert rows[3].origin_hotkey is None and rows[7].origin_hotkey is None


def test_wrong_validator_records_received_funds_without_credit():
    rows = read(*fixture(), validator=OTHER)
    assert len(rows) == 25 and sum(d.amount_rao for d in rows) == 25 * AMOUNT
    assert all("not the designated Ark Validator" in d.unattributed_reason for d in rows)


def test_approval_without_execution_or_receipts_is_not_a_deposit():
    xts = [{}, {}, {"call": call("Multisig", "approve_as_multi", call_hash="0xabc")}]
    assert read([], xts) == []


@pytest.mark.parametrize("count,multisig", [(1, False), (18, False), (32, True)])
def test_store_replay_and_entry_qualification_are_per_miner(tmp_path, count, multisig):
    rows = read(*fixture(count, multisig))
    store = Store.create(tmp_path / "receipts.db", day_anchor_ts=1_700_000_000)
    try:

        def record(row):
            values = asdict(row)
            values.pop("token_netuid")
            return store.record_noah_deposit(**values, block_timestamp=1_700_000_100)

        for _ in range(2):
            for row in rows:
                record(row)
        store.mark_day_scanned(0, from_block=100, to_block=100)
        with patch("noah.deposits.entry_gate.entry_deposit_rao", return_value=AMOUNT):
            assert [
                confirm_entry_before_score(store, COLD, m, as_of_day=0) for m in MINERS[:count]
            ] == [AMOUNT] * count
            with pytest.raises(EntryDepositNotConfirmed):
                confirm_entry_before_score(store, COLD, OTHER, as_of_day=0)
        assert store.noah_deposits(COLD, 0) == count * AMOUNT
        changed = copy.copy(rows[0])
        object.__setattr__(changed, "amount_rao", AMOUNT + 1)
        with pytest.raises(ObservationChanged):
            record(changed)
    finally:
        store.close()


def test_other_extrinsic_and_original_absolute_ordinals_are_preserved():
    events, xts = fixture(2)
    extra, other_xts = fixture(1)
    for event in extra:
        event["extrinsic_idx"] = 3
        event["ordinal_in_block"] += 100
    xts.append(other_xts[2])
    rows = read(events + extra, xts)
    assert [d.extrinsic_index for d in rows] == [2, 2, 3]
    assert [d.event_ordinal for d in rows] == [4, 10, 104]


@pytest.mark.parametrize("count,multisig", [(18, False), (32, True)])
@pytest.mark.parametrize("omit_zero", [False, True])
def test_full_pool_unequal_amounts_and_zero_omissions(count, multisig, omit_zero):
    amounts = [0 if omit_zero and i % 5 == 0 else AMOUNT + i for i in range(count)]
    rows = read(*fixture(count, multisig, amounts))
    assert [(r.origin_hotkey, r.amount_rao) for r in rows] == [
        (MINERS[i], amount) for i, amount in enumerate(amounts) if amount
    ]
    assert sum(r.amount_rao for r in rows) == sum(amounts)
    assert len({r.event_ordinal for r in rows}) == len(rows)


@pytest.mark.parametrize(
    "mutation",
    [
        "removed_source",
        "removed_position",
        "removed_amount",
        "removed_netuid",
        "removed_after_added",
        "duplicate_removed",
        "boolean_amount",
        "overflow_amount",
        "move_all_amount",
        "unexpected_batch_argument",
        "success_before_completion",
        "duplicate_success",
        "duplicate_multisig_execution",
        "events_out_of_order",
        "trailing_stake_event",
        "remark_in_transfer_segment",
    ],
)
def test_additional_malformed_evidence_cannot_credit(mutation):
    from noah.chain.deposit_batches import read_batch

    events, xts = fixture(32)
    if mutation.startswith("removed_") and mutation != "removed_after_added":
        index, value = {
            "removed_source": (0, OTHER),
            "removed_position": (1, OTHER),
            "removed_amount": (3, AMOUNT + 1),
            "removed_netuid": (4, 83),
        }[mutation]
        events[0]["attributes"][index] = value
    elif mutation == "removed_after_added":
        events[0]["event_id"], events[1]["event_id"] = "StakeAdded", "StakeRemoved"
        events[0]["attributes"], events[1]["attributes"] = (
            events[1]["attributes"],
            events[0]["attributes"],
        )
    elif mutation == "duplicate_removed":
        events.insert(1, copy.deepcopy(events[0]))
    elif mutation in {"boolean_amount", "overflow_amount", "move_all_amount"}:
        value = {
            "boolean_amount": True,
            "overflow_amount": 1 << 64,
            "move_all_amount": (1 << 64) - 1,
        }[mutation]
        inner_calls(xts)[0]["call_args"][-1]["value"] = value
    elif mutation == "unexpected_batch_argument":
        xts[2]["call"]["call_args"][0]["value"]["call_args"].append(
            {"name": "unknown", "value": []}
        )
    elif mutation == "success_before_completion":
        events[-1]["ordinal_in_block"] = 0
    elif mutation == "duplicate_success":
        events.append(copy.deepcopy(events[-1]))
    elif mutation == "duplicate_multisig_execution":
        events.insert(-1, copy.deepcopy(events[-2]))
    elif mutation == "events_out_of_order":
        events[0], events[1] = events[1], events[0]
    elif mutation == "trailing_stake_event":
        extra = copy.deepcopy(events[0])
        extra["ordinal_in_block"] = 1000
        events.append(extra)
    elif mutation == "remark_in_transfer_segment":
        events[2]["module_id"] = "System"
        events[2]["event_id"] = "Remarked"
        events[2]["attributes"] = events[4]["attributes"]
    assert (
        read_batch(
            xts[2],
            events,
            ark_coldkey=ARK,
            designated_validator_hotkey=VALIDATOR,
            block=100,
            block_hash="0xabc",
            parent_block_hash="0xparent",
            owner_source=Owner(),
        )
        is None
    )


def test_chain_owner_transport_refusal_is_not_a_settled_receipt():
    from noah.chain.live import ChainUnavailable

    class Unavailable:
        def owner_at(self, **kwargs):
            raise ChainUnavailable("archive disconnected")

    with pytest.raises(ChainUnavailable, match="archive disconnected"):
        read(*fixture(32), owner=Unavailable())


def test_rolled_back_atomic_batch_has_no_deposits():
    _, xts = fixture(32)
    events = [
        dict(
            module_id="System",
            event_id="ExtrinsicFailed",
            attributes={},
            extrinsic_idx=2,
            ordinal_in_block=7,
        )
    ]
    assert read(events, xts) == []


@pytest.mark.parametrize('result', [
    {'Ok': [0]}, {'Ok': (0,)}, {'Ok': ''}, {'Ok': False}, {'Ok': 0},
    {'Ok': {}}, {'Err': None}, {'Ok': (), 'Err': None}, {'Ok': (), 'extra': 1},
])
def test_nonunit_or_ambiguous_multisig_result_never_credits(result):
    events, xts = fixture(2, True)
    next(e for e in events if e['event_id'] == 'MultisigExecuted')['attributes']['result'] = result
    assert all(d.unattributed_reason is not None for d in read(events, xts))


@pytest.mark.parametrize('unit', [None, [], ()], ids=['null', 'list', 'tuple'])
@pytest.mark.parametrize('multisig', [False, True])
def test_remarked_batch_unit_success_and_unequal_arrivals(unit, multisig):
    events, extrinsics = fixture(2, multisig, [AMOUNT, AMOUNT + 17])
    if multisig:
        events[-2]['attributes']['result'] = {'Ok': unit}
    rows = read(events, extrinsics)
    assert [(r.event_ordinal, r.amount_rao) for r in rows] == [(4, AMOUNT), (10, AMOUNT + 17)]
    assert [r.origin_hotkey for r in rows] == MINERS[:2]
    assert all(r.unattributed_reason is None for r in rows)


def transfer_only_fixture(*, named=True, token=82, basket=False, unit=None):
    """Construct decoded-shaped input; the hash is a synthetic opaque binding.

    It intentionally is not a claimed SDK decode or a signed transaction.
    """
    events, legs = [], []
    call_hash = '0x' + hashlib.blake2b(b'public synthetic batch', digest_size=32).hexdigest()

    def event(module, name, attributes):
        events.append(dict(module_id=module, event_id=name, attributes=attributes,
                           ordinal_in_block=100 + len(events), extrinsic_idx=2))

    for index, amount in enumerate([AMOUNT, AMOUNT + 17]):
        origin = MINERS[index] if named else VALIDATOR
        fields = dict(destination_coldkey=ARK, origin_netuid=token,
                      destination_netuid=token, alpha_amount=amount)
        fields.update(dict(origin_hotkey=origin, destination_hotkey=VALIDATOR)
                      if named else dict(hotkey=VALIDATOR))
        legs.append(call('SubtensorModule', 'transfer_stake_and_hotkey' if named
                         else 'transfer_stake', **fields))
        if basket:
            event('SubtensorModule', 'BasketDeposited', dict(hotkey=origin, shares=7, tao=11))
        event('SubtensorModule', 'StakeRemoved', [COLD, origin, amount, amount, token, 0])
        event('SubtensorModule', 'StakeAdded', [ARK, VALIDATOR, amount, amount, token, 0])
        if named:
            moved = dict(origin_coldkey=COLD, destination_coldkey=ARK,
                         origin_hotkey=origin, destination_hotkey=VALIDATOR,
                         origin_netuid=token, destination_netuid=token, amount=123)
        else:
            moved = [COLD, ARK, VALIDATOR, token, token, 123]
        # 123 is TAO equivalent, deliberately different from arriving alpha.
        event('SubtensorModule', 'StakeAndHotkeyTransferred' if named else 'StakeTransferred', moved)
        event('Utility', 'ItemCompleted', None)
    event('Utility', 'BatchCompleted', None)
    event('Multisig', 'MultisigExecuted', dict(multisig=COLD, result={'Ok': unit}, call_hash=call_hash))
    event('System', 'ExtrinsicSuccess', {})
    inner = call('Utility', 'batch_all', calls=legs)
    inner['call_hash'] = call_hash
    return events, [{}, {}, dict(address=SIGNER, call=call('Multisig', 'as_multi', call=inner))]


def batch_inner(extrinsics):
    return extrinsics[2]['call']['call_args'][0]['value']


def event_named(events, name):
    return next(e for e in events if e['event_id'] == name)


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('token', [0, 19, 82])
@pytest.mark.parametrize('basket', [False, True])
@pytest.mark.parametrize('unit', [None, [], ()], ids=['null', 'list', 'tuple'])
def test_transfer_only_preserves_arrivals_and_attribution(named, token, basket, unit):
    events, extrinsics = transfer_only_fixture(named=named, token=token, basket=basket, unit=unit)
    rows = read(events, extrinsics)
    arrivals = [e for e in events if e['event_id'] == 'StakeAdded']
    assert len(rows) == 2
    assert [(r.event_ordinal, r.amount_rao) for r in rows] == [
        (e['ordinal_in_block'], e['attributes'][3]) for e in arrivals]
    assert [r.amount_rao for r in rows] == [AMOUNT, AMOUNT + 17]
    assert all(r.token_netuid == token and r.coldkey == COLD and r.extrinsic_index == 2 for r in rows)
    if named:
        assert [r.origin_hotkey for r in rows] == MINERS[:2]
        assert all(r.owner_at_deposit == COLD and r.unattributed_reason is None for r in rows)
    else:
        assert all(r.origin_hotkey is None and r.owner_at_deposit is None for r in rows)
        assert all('names no miner' in r.unattributed_reason for r in rows)


@pytest.mark.parametrize('named', [False, True])
def test_transfer_only_wrong_validator_never_gets_credit(named):
    rows = read(*transfer_only_fixture(named=named), validator=OTHER)
    assert len(rows) == 2
    assert all('not the designated Ark Validator' in r.unattributed_reason for r in rows)


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('token', [0, 82])
def test_transfer_only_store_replay_is_idempotent(tmp_path, named, token):
    from noah.chain.runner import _record

    rows = read(*transfer_only_fixture(named=named, token=token))
    store = Store.create(tmp_path / 'transfer-only.db', day_anchor_ts=1_700_000_000)
    try:
        table = 'noah_deposit' if token == 82 else 'foreign_deposit'
        for r in rows:
            _record(store, r, 1_700_000_100, 82)
        before = list(store._conn.execute('SELECT * FROM ' + table + ' ORDER BY event_ordinal'))
        for r in rows:
            _record(store, r, 1_700_000_100, 82)
        after = list(store._conn.execute('SELECT * FROM ' + table + ' ORDER BY event_ordinal'))
        assert before == after and len(after) == 2
        assert store._conn.execute('SELECT SUM(amount_rao) FROM ' + table).fetchone()[0] == 2 * AMOUNT + 17
        assert not store.day_was_scanned(0)
    finally:
        store.close()


@pytest.mark.parametrize('result', [
    {'Err': None}, {'Ok': [0]}, {'Ok': (0,)}, {'Ok': ''}, {'Ok': False},
    {'Ok': 0}, {'Ok': {}}, {'Ok': None, 'Err': None}, {'Ok': None, 'extra': 1},
])
def test_transfer_only_rejects_nonunit_success(result):
    events, extrinsics = transfer_only_fixture()
    event_named(events, 'MultisigExecuted')['attributes']['result'] = result
    with pytest.raises(NotADepositEvent):
        read(events, extrinsics)


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('fault', [
    'missing_added', 'duplicate_added', 'missing_removed', 'missing_item',
    'duplicate_ordinal', 'wrong_giver', 'wrong_amount', 'wrong_token',
    'wrong_destination', 'wrong_origin', 'call_hash', 'missing_call_hash',
    'invalid_call_hash', 'multisig_failure', 'extrinsic_failure',
    'batch_interrupted', 'extra_arrival', 'wrong_removed_amount',
    'wrong_removed_token', 'wrong_removed_owner', 'wrong_removed_position',
    'removed_shape', 'added_shape', 'moved_shape', 'unexpected_call_argument',
    'unexpected_batch_argument', 'boolean_amount', 'move_all', 'oversized_amount',
    'negative_tao', 'boolean_token', 'oversized_token', 'success_before_execution',
    'duplicate_execution', 'duplicate_success', 'two_extrinsic_indexes',
])
def test_transfer_only_conflicting_evidence_refuses_whole_batch(named, fault):
    events, extrinsics = transfer_only_fixture(named=named)
    inner = batch_inner(extrinsics)
    args = {a['name']: a for a in inner_calls(extrinsics)[0]['call_args']}
    added = event_named(events, 'StakeAdded')
    removed = event_named(events, 'StakeRemoved')
    moved = event_named(events, 'StakeAndHotkeyTransferred' if named else 'StakeTransferred')
    execution = event_named(events, 'MultisigExecuted')
    if fault == 'missing_added':
        events.remove(added)
    elif fault == 'duplicate_added':
        events.insert(events.index(added), copy.deepcopy(added))
    elif fault == 'missing_removed':
        events.remove(removed)
    elif fault == 'missing_item':
        events.remove(event_named(events, 'ItemCompleted'))
    elif fault == 'duplicate_ordinal':
        moved['ordinal_in_block'] = added['ordinal_in_block']
    elif fault == 'wrong_giver':
        execution['attributes']['multisig'] = OTHER
    elif fault == 'wrong_amount':
        added['attributes'][3] += 1
    elif fault == 'wrong_token':
        added['attributes'][4] += 1
    elif fault in ('wrong_destination', 'wrong_origin'):
        if named:
            moved['attributes']['destination_hotkey' if fault == 'wrong_destination' else 'origin_hotkey'] = OTHER
        else:
            moved['attributes'][2 if fault == 'wrong_destination' else 0] = OTHER
    elif fault == 'call_hash':
        inner['call_hash'] = '0x' + '00' * 32
    elif fault == 'missing_call_hash':
        del inner['call_hash']
    elif fault == 'invalid_call_hash':
        inner['call_hash'] = '0x' + 'zz' * 32
    elif fault == 'multisig_failure':
        execution['attributes']['result'] = {'Err': 'synthetic failure'}
    elif fault == 'extrinsic_failure':
        events[-1]['event_id'] = 'ExtrinsicFailed'
    elif fault == 'batch_interrupted':
        event_named(events, 'BatchCompleted')['event_id'] = 'BatchInterrupted'
    elif fault == 'extra_arrival':
        extra = copy.deepcopy(added)
        extra['ordinal_in_block'] = events[-1]['ordinal_in_block'] + 1
        events.append(extra)
    elif fault.startswith('wrong_removed_'):
        position, value = {'amount': (3, AMOUNT + 1), 'token': (4, 19),
                           'owner': (0, OTHER), 'position': (1, OTHER)}[fault.removeprefix('wrong_removed_')]
        removed['attributes'][position] = value
    elif fault in ('removed_shape', 'added_shape', 'moved_shape'):
        {'removed_shape': removed, 'added_shape': added, 'moved_shape': moved}[fault]['attributes'] = {}
    elif fault == 'unexpected_call_argument':
        inner_calls(extrinsics)[0]['call_args'].append(dict(name='unexpected', value=1))
    elif fault == 'unexpected_batch_argument':
        inner['call_args'].append(dict(name='unexpected', value=1))
    elif fault in ('boolean_amount', 'move_all', 'oversized_amount'):
        args['alpha_amount']['value'] = {'boolean_amount': True, 'move_all': (1 << 64)-1,
                                         'oversized_amount': 1 << 64}[fault]
    elif fault == 'negative_tao':
        if named:
            moved['attributes']['amount'] = -1
        else:
            moved['attributes'][5] = -1
    elif fault in ('boolean_token', 'oversized_token'):
        value = True if fault == 'boolean_token' else 65536
        args['origin_netuid']['value'] = args['destination_netuid']['value'] = value
    elif fault == 'success_before_execution':
        events[-1]['ordinal_in_block'] = execution['ordinal_in_block'] - 1
    elif fault == 'duplicate_execution':
        events.insert(-1, copy.deepcopy(execution))
    elif fault == 'duplicate_success':
        events.append(copy.deepcopy(events[-1]))
    elif fault == 'two_extrinsic_indexes':
        added['extrinsic_idx'] += 1
    with pytest.raises(NotADepositEvent):
        read(events, extrinsics)


@pytest.mark.parametrize('fault', ['duplicate', 'misplaced', 'wrong_hotkey', 'negative', 'boolean', 'extra_key'])
def test_transfer_only_basket_prelude_rejects_invalid_accounting(fault):
    events, extrinsics = transfer_only_fixture(basket=True)
    basket = event_named(events, 'BasketDeposited')
    if fault == 'duplicate':
        events.insert(0, copy.deepcopy(basket))
    elif fault == 'misplaced':
        events[0], events[1] = events[1], events[0]
    elif fault == 'wrong_hotkey':
        basket['attributes']['hotkey'] = OTHER
    elif fault == 'negative':
        basket['attributes']['shares'] = -1
    elif fault == 'boolean':
        basket['attributes']['tao'] = True
    else:
        basket['attributes']['amount'] = 1
    with pytest.raises(NotADepositEvent):
        read(events, extrinsics)
