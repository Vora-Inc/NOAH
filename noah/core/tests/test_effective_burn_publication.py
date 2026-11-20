"""Offline direct publication/recovery with real proof reader and retained records."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from noah.chain import burn_context as burn
from noah.chain.sources import Refusal
from noah.chain.tests.test_burn_context import FRAME, GENESIS
from noah.core.burn_allocation import ENABLED
from noah.core.burn_publication import recompute, report_subject, wrap_allocation
from noah.core.calculation_record import artifact, retain, verify
from noah.core.publish import PublicationRefused
from noah.core.reader import ChainUnreadable
from ._prepared_transport_fake import PreparedTransportFake
from .test_direct_runtime475 import VECTOR, adapter
from .test_epoch_work import EpochClient, configure


class BurnRPC:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.raw = None
        self.code = burn.REVIEWED[492][0]
        self.metadata = FRAME
        self.fail = False

    def __call__(self, method, args):
        self.calls.append((method, args))
        pin = lambda height: f'0x{height:064x}'
        if self.fail:
            raise OSError('private endpoint sentinel')
        if method == 'chain_getFinalizedHead':
            return pin(self.client.finalized_head)
        if method == 'chain_getHeader':
            return {'number': hex(int(args[0], 16))}
        if method == 'chain_getBlockHash':
            return GENESIS if args == [0] else pin(args[0])
        assert 0 < int(args[-1], 16) <= self.client.finalized_head
        if method == 'state_getRuntimeVersion':
            return {'specVersion': 492, 'transactionVersion': 1}
        if method == 'state_getStorageHash':
            return self.code
        if method == 'state_call':
            return self.metadata
        assert method == 'state_queryStorageAt'
        assert args[0] == [burn.storage_key(82)]
        return [{'block': args[-1], 'changes': [[burn.storage_key(82), self.raw]]}]


def ready(tmp_path):
    built, client = adapter(tmp_path, EpochClient())
    rpc = BurnRPC(client)
    old_rpc = client.rpc_request
    async def raw_request(method, params):
        if method == 'chain_getBlock':
            return await old_rpc(method, params)
        return rpc(method, params)
    client.rpc_request = raw_request
    built.day_anchor_ts = 0
    built.runner_chain.genesis_hash = lambda: GENESIS
    built.runner_chain.block_timestamp = lambda h: 86400 + h
    built.runner_chain.seat_hotkeys = lambda h: {0: 'owner-hot', 3: 'a', 4: 'b', 5: 'c'}
    built.runner_chain.subnet_owner = lambda block: ('owner-cold', 'owner-hot')
    built.runner_chain.hotkey_owners = lambda keys, block: {key: 'owner-cold' for key in keys}
    built.runner_chain.burn_context = lambda block, netuid, expected_block_hash: burn.read(
        rpc, block, expected_block_hash, netuid)
    built.owner_uids = lambda: {0}
    built.reader.recycle_or_burn = lambda netuid: None
    built.reader.max_weights_limit = lambda netuid: 65535
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: 'Yuma'
    built._calculation_reference = retain(
        built._submitted_record_dir,
        fixture={'schema': 'noah-fixture/6', 'evalDay': 0},
        agreed_text=json.dumps({'schema': 'noah-score-output/6', 'evalDay': 0,
                               'weightsU16': {str(k): v for k, v in VECTOR.items()}}),
        vector=VECTOR,
        context={'day': 0, 'height': 99, 'to_block': 99, 'to_block_hash': f'0x{99:064x}',
                 'genesis_hash': GENESIS, 'netuid': 82, 'ark_address': built.ark_coldkey,
                 'designated_validator_hotkey': built.designated_validator_hotkey,
                 'scoring_dials': built.dials, 'day_start_utc_unix': 0})
    built.burn_allocation_bps = ENABLED
    built.scoring_mode = 'points-204040-v5'
    published = built.prepare_burn_vector(dict(VECTOR))
    # This fixture exercises the direct transport and burn records; the existing
    # V2 suite owns scoring/accounting. Do not invent a fake V2 settlement here.
    built.scoring_mode = 'legacy'
    return built, client, rpc, published


@pytest.mark.parametrize('failure', ['recycle', 'code', 'metadata', 'unreadable', 'author', 'owner'])
def test_actual_send_boundary_refuses_changed_evidence_before_prepare(tmp_path, monkeypatch, failure):
    built, client, rpc, published = ready(tmp_path)
    prepare = Mock(wraps=PreparedTransportFake.prepare)
    monkeypatch.setattr(PreparedTransportFake, 'prepare', prepare)
    if failure == 'recycle':
        rpc.raw = '0x01'
    elif failure == 'code':
        rpc.code = '0x' + 'ff' * 32
    elif failure == 'metadata':
        rpc.metadata = '0x00'
    elif failure == 'unreadable':
        rpc.fail = True
    elif failure == 'owner':
        built.runner_chain.hotkey_owners = lambda keys, block: {key: 'wrong-cold' for key in keys}
    else:
        original = built.reader.burn_context
        def disagree(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(raw='0x00', origin='explicit')
            return result
        built.reader.burn_context = disagree
    with pytest.raises((Refusal, PublicationRefused, ChainUnreadable, OSError)):
        built.publish_direct(published, 99)
    assert prepare.call_count == 0 and client.calls == []
    assert built._publish_intent.load() is None


def test_fresh_advanced_pin_succeeds_without_rewriting_calculation_proof(tmp_path):
    built, client, rpc, published = ready(tmp_path)
    reference = built._calculation_reference
    manifest = verify(built._submitted_record_dir, reference)
    before = artifact(built._submitted_record_dir, manifest, 'context')
    original_pin = before['burn_allocation']['burn_context']['block_hash']
    client.block = client.finalized_head = 110
    rpc.calls.clear()
    result = built.publish_direct(published, 99)
    assert result['applied_weights_readback']['confirmed'] is True
    assert len(client.calls) == 1
    assert all(args[-1] == f'0x{110:064x}' for method, args in rpc.calls if method.startswith('state_'))
    assert artifact(built._submitted_record_dir, manifest, 'context') == before
    assert original_pin != f'0x{110:064x}'


def test_default_receipt_restart_and_burn_zero_preserve_history(tmp_path):
    built, client, rpc, published = ready(tmp_path)
    reference = built._calculation_reference
    source, pre = report_subject(built._submitted_record_dir, reference, published)
    built.publish_direct(published, 99)
    original = (tmp_path / 'scores/day-0.submitted.json').read_bytes()
    restarted, _ = adapter(tmp_path, client)
    restarted.runner_chain = built.runner_chain
    restarted.day_anchor_ts = built.day_anchor_ts
    restarted.owner_uids = built.owner_uids
    configure(restarted, SimpleNamespace(epoch_republication=True, epoch_collection=False))
    restarted.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    # Burn-zero must not require supported default state or any new proof reads.
    restarted.reader.recycle_or_burn = lambda _: pytest.fail('burn-zero queried Burn')
    from noah.core.epoch_work import republish
    assert republish(restarted, 200, 201) == 'burn_cleared'
    assert (tmp_path / 'scores/day-0.submitted.json').read_bytes() == original
    path, = (tmp_path / 'scores/burn-transitions/day-0').glob('*/day-0.submitted.json')
    removed = json.loads(path.read_text())['direct_intent']['calculation_evidence']
    assert recompute(restarted._submitted_record_dir, removed, pre) is None
    assert report_subject(restarted._submitted_record_dir, removed, pre) == (source, pre)
    assert recompute(restarted._submitted_record_dir, reference, published) == 0


@pytest.mark.parametrize('initial_explicit', [False, True])
def test_explicit_and_default_transitions_use_fresh_checks(tmp_path, initial_explicit):
    built, client, rpc, published = ready(tmp_path)
    if initial_explicit:
        # An old schema-1 record has no default context and remains readable.
        manifest = verify(built._submitted_record_dir, built._calculation_reference)
        context = artifact(built._submitted_record_dir, manifest, 'context')
        body = context['burn_allocation']
        source = body['source']
        old_body = {k: v for k, v in body.items() if k not in ('burn_context', 'source', 'schema')}
        built._calculation_reference = wrap_allocation(built._submitted_record_dir, source, published, old_body)
        assert recompute(built._submitted_record_dir, built._calculation_reference, published) == 0
        # Current absence must still take the new live proof path.
    else:
        built.reader.recycle_or_burn = lambda _: 'Burn'
        built.reader.burn_context = lambda *a, **k: pytest.fail('explicit path queried default proof')
        rpc.raw = '0x00'
    assert built.publish_direct(published, 99)['applied_weights_readback']['confirmed'] is True
    assert len(client.calls) == 1


def test_author_bool_cannot_compare_equal_to_valid_integer_proof(tmp_path, monkeypatch):
    built, client, rpc, published = ready(tmp_path)
    original = built.reader.burn_context
    def malformed(*args, **kwargs):
        result = original(*args, **kwargs)
        result['transaction_version'] = True
        return result
    built.reader.burn_context = malformed
    prepare = Mock()
    monkeypatch.setattr(PreparedTransportFake, 'prepare', prepare)
    with pytest.raises(Refusal):
        built.publish_direct(published, 99)
    assert prepare.call_count == 0 and client.calls == []


def test_author_transport_errors_are_sanitized_not_defaulted():
    import asyncio
    from noah.chain.tests.test_burn_context import BLOCK, PIN
    from noah.core.reader import ChainReader, ChainUnreadable
    async def fail(method, args):
        raise OSError('private endpoint sentinel')
    raw = SimpleNamespace(rpc_request=fail)
    client = SimpleNamespace(_client=SimpleNamespace(_substrate=SimpleNamespace(raw=raw)), _call=asyncio.run)
    with pytest.raises(ChainUnreadable) as error:
        ChainReader(client).burn_context(BLOCK, 82, expected_block_hash=PIN)
    assert 'private endpoint sentinel' not in str(error.value)
