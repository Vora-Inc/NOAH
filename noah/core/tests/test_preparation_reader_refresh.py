"""Pre-sign reader refresh is refusal, never a same-attempt signing retry."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.live import LiveRunnerChain
from noah.chain.read_connection import ReadSubstrateInterface
from noah.core.prepared_transaction import SDKPreparedTransport
from noah.core.publication_diagnostics import failure_details
from noah.core.reader import ChainUnreadable
from .test_prepared_transaction import Chain, Client, Wallet


class Socket:
    close_code = None

    def __init__(self, head):
        self.head, self.sent, self.closes = head, [], 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, **kwargs):
        req = self.sent[-1]
        result = {"number": hex(self.head)} if req["method"] == "chain_getHeader" else f"0x{self.head:064x}"
        return json.dumps(dict(id=req["id"], result=result)).encode()

    def close(self):
        self.close_code = 1000
        self.closes += 1


def actual_readers(head):
    sources, sockets = {}, []
    for name in ("primary", "secondary"):
        sock = Socket(head)
        raw = ReadSubstrateInterface(url=f"ws://{name}.invalid", _mock=True)
        raw.ws = sock
        sources[name] = LiveRunnerChain(raw, netuid=82, endpoint=f"ws://{name}.invalid")
        sockets.append(sock)
    return AgreeingChain(sources, {name: source.endpoint for name, source in sources.items()}), sockets


def test_real_reader_sessions_close_then_next_normal_attempt_rechecks_and_signs_once():
    paired, old = actual_readers(100)
    submit_chain, wallet = Chain(), Wallet()
    submit_chain.head = 110
    paired.genesis_hash = submit_chain.genesis_hash
    paired.signing_context = submit_chain.signing_context
    paired.block_hash = submit_chain.block_hash
    client = Client(submit_chain)
    transport = SDKPreparedTransport(client, wallet)
    with pytest.raises(ChainUnreadable) as caught:
        transport.prepare(b"call", paired)
    details = failure_details(caught.value)
    assert details['preparation_predicate'] == 'mortality'
    assert details['preparation_heads'] == dict(before=100, prepared=110, after=100)
    assert details['reader_refresh'] == 'closed'
    assert all(s.closes == 1 for s in old)
    assert wallet.signatures == 0 and not client.sent and len(client.prepared) == 1
    fresh = [Socket(110), Socket(110)]
    with patch('async_substrate_interface.sync_substrate.connect', side_effect=fresh):
        prepared = transport.prepare(b"call", paired)
        assert wallet.signatures == 1 and len(client.prepared) == 2
        assert transport.send(prepared, paired) == 'accepted'
    assert len(client.sent) == 1
    paired.close()
    assert all(s.closes == 1 for s in fresh)


def mismatch(*, nonce=False, refresh_error=False, still_stale=False):
    chain, submit_chain, wallet = Chain(), Chain(), Wallet()
    submit_chain.head = 110
    if nonce:
        submit_chain.nonce = chain.nonce + 1
    closed = []
    def refresh():
        closed.append(True)
        if refresh_error:
            raise OSError('PRIVATE_CLOSE_DETAILS')
        if not still_stale:
            chain.head = submit_chain.head
    chain.refresh_read_connections = refresh
    client = Client(submit_chain)
    return chain, client, wallet, closed


@pytest.mark.parametrize('nonce', [False, True])
def test_nonce_precedence_preserved_but_validated_ahead_context_can_close_readers(nonce):
    chain, client, wallet, closed = mismatch(nonce=nonce)
    with pytest.raises(ChainUnreadable) as caught:
        SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    assert failure_details(caught.value)['preparation_predicate'] == ('nonce' if nonce else 'mortality')
    assert closed == [True] and wallet.signatures == 0 and not client.sent


def test_reset_failure_is_explicit_refusal_no_sign_or_send():
    chain, client, wallet, closed = mismatch(refresh_error=True)
    with pytest.raises(ChainUnreadable) as caught:
        SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    details = failure_details(caught.value)
    assert details['reader_refresh'] == 'failed' and 'PRIVATE' not in json.dumps(details)
    assert closed == [True] and wallet.signatures == 0 and not client.sent


def test_genuine_upstream_lag_refuses_every_normal_attempt_without_signing():
    chain, client, wallet, closed = mismatch(still_stale=True)
    for _ in range(2):
        with pytest.raises(ChainUnreadable):
            SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    assert len(closed) == 2 and len(client.prepared) == 2
    assert wallet.signatures == 0 and not client.sent


@pytest.mark.parametrize('field,value', [
    ('address', 'wrong'), ('genesis_hash', 'wrong'), ('spec_version', 487),
    ('transaction_version', True), ('era', {'period':128, 'current':True}),
    ('era', {'period':128, 'current':-1}), ('era_block_hash', 'PRIVATE_BAD_HASH'),
    ('call_data', b'wrong'), ('nonce', True), ('nonce', 2**64),
])
def test_invalid_unsigned_context_never_authorizes_refresh(field, value):
    chain, client, wallet, closed = mismatch()
    original = client.prepare_call
    def changed(*args, **kwargs):
        unsigned = original(*args, **kwargs)
        setattr(unsigned, field, value)
        return unsigned
    client.prepare_call = changed
    with pytest.raises(ChainUnreadable):
        SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    assert not closed and wallet.signatures == 0 and not client.sent


def test_nonce_mismatch_alone_never_closes_readers():
    chain, client, wallet, closed = mismatch(nonce=True)
    client.chain.head = chain.head
    with pytest.raises(ChainUnreadable) as caught:
        SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    assert failure_details(caught.value)['preparation_predicate'] == 'nonce'
    assert not closed and wallet.signatures == 0 and not client.sent


def test_both_sources_close_even_if_first_close_fails():
    closed = []
    def fail():
        closed.append('primary')
        raise OSError('PRIVATE_CLOSE_DETAILS')
    sources = {'primary':SimpleNamespace(refresh_read_connection=fail),
               'secondary':SimpleNamespace(refresh_read_connection=lambda:closed.append('secondary'))}
    chain = AgreeingChain(sources, {'primary':'ws://a.invalid','secondary':'ws://b.invalid'})
    with pytest.raises(OSError):
        chain.refresh_read_connections()
    assert closed == ['primary','secondary']


def test_preparation_diagnostics_reject_untrusted_labels_heads_and_refresh_values():
    error = ChainUnreadable('PRIVATE_ERROR')
    error._noah_preparation_predicate = 'PRIVATE_LABEL'
    error._noah_preparation_heads = {'before':1,'prepared':2,'after':1}
    error._noah_reader_refresh = 'closed'
    assert 'preparation_heads' not in failure_details(error)
    error._noah_preparation_predicate = 'mortality'
    error._noah_preparation_heads['before'] = True
    error._noah_reader_refresh = 'PRIVATE_LABEL'
    details = failure_details(error)
    assert details['preparation_predicate'] == 'mortality'
    assert 'preparation_heads' not in details and 'reader_refresh' not in details
    assert 'PRIVATE' not in json.dumps(details)


def test_next_attempt_still_refuses_unchanged_nonce_disagreement_after_fresh_heads():
    chain, client, wallet, closed = mismatch(nonce=True)
    for attempt in range(2):
        with pytest.raises(ChainUnreadable) as caught:
            SDKPreparedTransport(client, wallet).prepare(b'call', chain)
        assert failure_details(caught.value)['preparation_predicate'] == 'nonce'
    assert closed == [True] and len(client.prepared) == 2
    assert wallet.signatures == 0 and not client.sent


@pytest.mark.parametrize('context', [None, {'nonce':True,'specVersion':486,'transactionVersion':1},
                                    {'nonce':215,'specVersion':486,'transactionVersion':1,'extra':0}])
def test_malformed_finalized_context_does_not_prepare_or_refresh(context):
    chain, client, wallet, closed = mismatch()
    chain.signing_context = lambda *args:context
    with pytest.raises(ChainUnreadable):
        SDKPreparedTransport(client, wallet).prepare(b'call', chain)
    assert not client.prepared and not closed and wallet.signatures == 0


def test_real_guard_records_refusal_and_next_tick_uses_existing_publication_gates(tmp_path):
    from noah.core.publish import PublicationRefused
    from noah.core.tests._prepared_transport_fake import PreparedTransportFake
    from noah.starter.tests.test_guard_diagnostics import expired, health
    from noah.starter.runtime import guard_tick
    a, applied = expired(tmp_path)
    chain, client, wallet, closed = mismatch()
    class FailedPreparation:
        def __init__(self, *_args):
            pass
        def prepare(self, _call, _chain):
            return SDKPreparedTransport(client, wallet).prepare(b'call', chain)
        def send(self, *_args):
            pytest.fail('refused attempt must never reach send')
    a.publisher.transport_factory = FailedPreparation
    before = len(applied.calls)
    with pytest.raises(PublicationRefused):
        guard_tick(a)
    details = health(tmp_path)
    assert details['status'] == 'blocked' and details['stage'] == 'publish_fallback'
    assert details['preparation_predicate'] == 'mortality' and details['reader_refresh'] == 'closed'
    assert a._publish_intent.load() is None
    assert len(applied.calls) == before and wallet.signatures == 0 and closed == [True]
    # A separately scheduled normal tick, with all retained-proof/identity/nonce
    # gates intact, may publish. The failed tick did not retry internally.
    a.publisher.transport_factory = PreparedTransportFake
    assert guard_tick(a)['status'] == 'replaced'
    assert len(applied.calls) == before + 1
