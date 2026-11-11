"""Real direct reconciliation through the read-only SDK envelope bridge.

Retained graph verification is mocked in these narrow transport tests; direct
call, proxy event, prepared-byte, finalized hash and current storage checks run.
"""
import copy
import json
from types import SimpleNamespace

import pytest

from noah.core.tests.test_direct_runtime475 import adapter as direct_adapter, VECTOR
from noah.core.tests._prepared_transport_fake import install
from noah.points import transition
from noah.chain.live import ChainUnavailable
from noah.core.reader import ChainUnreadable


class Wrapped:
    def __init__(self, value): self.value = value


@pytest.fixture
def current(tmp_path, monkeypatch):
    from noah.core import prepared_transaction
    real_signed_bytes_at = prepared_transaction.signed_bytes_at
    install(monkeypatch)
    adapter, client = direct_adapter(tmp_path)
    adapter.publish_direct(VECTOR, 99)
    monkeypatch.setattr(prepared_transaction, "signed_bytes_at", real_signed_bytes_at)
    marker = json.loads((tmp_path/'scores/day-0.submitted.json').read_bytes())['direct_intent']
    reference = {'synthetic': 'graph-boundary-tested-separately'}
    root = tmp_path/'scores'
    (root/'starter-active.json').write_text(json.dumps({'reference':reference,'marker':marker}))
    monkeypatch.setattr(transition,'verify',lambda *_: {'publishable_weights_u16':marker['vector'],'day':marker['day']})
    monkeypatch.setattr(transition,'verify_overlay',lambda *_: {'mode':'ordinary-fallback'})
    monkeypatch.setattr('noah.points.mode.source_mode',lambda *_: ('legacy',None,None))
    class Connection:
        error = False
        changed = False
        bad_bytes = False
        bad_key = False
        bad_weights = False
        def query(self,pallet,name,params,block_hash):
            block=int(block_hash,16)
            value = client.query((pallet,name),params,block=block)
            if name=='LastUpdate': value=[102 if self.changed else 101]
            if name=='Keys' and self.bad_key:value=marker['signer']
            if name=='Weights' and self.bad_weights:value=[(0,65535)]
            return Wrapped(value)
        def get_block(self,block_hash):
            return {'extrinsics':[Wrapped(x) for x in client.extrinsics]}
        def rpc_request(self,method,params):
            assert method=='chain_getBlock'
            if self.error: return {'error': {'code':-32000,'message':'unavailable'}}
            data=b'wrong' if self.bad_bytes else b'offline-test-extrinsic'
            return {'jsonrpc':'2.0','id':1,'result':{'block':{'extrinsics':['0x'+data.hex()]*3}}}
    connections=[Connection(),Connection()]
    chain=SimpleNamespace(head_block=lambda:101,block_hash=lambda b:f'0x{b:064x}',
        _sources={str(i):SimpleNamespace(_connection=c,block_hash=lambda b:f'0x{b:064x}') for i,c in enumerate(connections)})
    adapter.runner_chain=chain
    return adapter,root,connections,client,marker


def test_proxy_prepared_bytes_sdk_envelope_wrappers_and_string_keys(current):
    adapter,root,connections,client,marker=current
    assert marker['prepared_transaction']['extrinsic_hash']
    assert marker['route']=='proxy' and marker['vector']['0']==0
    result=transition.confirm_legacy_current(adapter,root)
    assert result['last_update']==101
    assert len(client.calls)==1  # setup only; confirmation cannot send


@pytest.mark.parametrize('fault',['error','bad_bytes','changed','bad_key','bad_weights'])
def test_one_reader_error_wrong_bytes_or_new_publication_refuses(current,fault):
    adapter,root,connections,_,_=current
    setattr(connections[1],fault,True)
    with pytest.raises((ValueError,ChainUnavailable,ChainUnreadable)):
        transition.confirm_legacy_current(adapter,root)


def test_proxy_inner_failure_refuses(current):
    adapter,root,_,client,_=current
    client.events[1]['event']['attributes']={'result':{'Err':'NotProxy'}}
    with pytest.raises(Exception): transition.confirm_legacy_current(adapter,root)


def test_missing_current_marker_refuses(current):
    adapter,root,*_=current
    (root/'starter-active.json').unlink()
    with pytest.raises(FileNotFoundError): transition.confirm_legacy_current(adapter,root)


def transition_state(tmp_path, monkeypatch):
    from noah.points.tests.test_integration import captured
    from noah.points import amendment
    from noah.core.calculation_record import encoded
    _,old=captured(tmp_path/'capture')
    original=json.dumps(old,indent=2).encode()+b'\n'
    doc=amendment.build(original)
    root=tmp_path/'runtime/ticket-16/scores';root.mkdir(parents=True)
    configured=root.parent.parent/'points-policy.json';configured.write_bytes(original)
    adapter=SimpleNamespace(_submitted_record_dir=root,_publish_intent=SimpleNamespace(load=lambda:None),
                            _commit_ledger=SimpleNamespace(load=lambda:{}))
    monkeypatch.setattr(transition,'confirm_legacy_current',lambda *_: {'tested':'separately-real-transport'})
    return adapter,root,configured,old,doc,original


def test_transition_preserves_noncanonical_original_and_canonical_old_durable(tmp_path,monkeypatch):
    from noah.points import amendment
    from noah.core.calculation_record import encoded
    adapter,root,configured,old,doc,original=transition_state(tmp_path,monkeypatch)
    assert original!=encoded(old)
    for installed in (None,encoded(old)):
        if installed is not None:(root/'points-policy.json').write_bytes(installed)
        before={p:p.read_bytes() for p in root.parent.parent.rglob('*') if p.is_file()}
        assert amendment.check_transition(adapter,doc)==old
        assert before=={p:p.read_bytes() for p in root.parent.parent.rglob('*') if p.is_file()}
    (root/'points-policy-predecessor.json').write_bytes(original)
    assert amendment.check_transition(adapter,doc)==old # interrupted archive-first state
    (root/'points-policy.json').write_bytes(encoded(doc))
    assert amendment.check_transition(adapter,doc)==old # journaled resume only in release
    configured.write_bytes(encoded(old))
    with pytest.raises(ValueError,match='original configured'):amendment.check_transition(adapter,doc)


@pytest.mark.parametrize('fault',['pending','postcutoff','unknown','archive','successor-no-archive'])
def test_transition_refuses_pending_prior_send_or_wrong_state(tmp_path,monkeypatch,fault):
    from noah.points import amendment
    from noah.core.calculation_record import encoded
    adapter,root,_,old,doc,original=transition_state(tmp_path,monkeypatch)
    if fault=='pending':adapter._publish_intent.load=lambda:{'pending':True}
    elif fault=='postcutoff':(root/'day-999.submitted.json').write_text(json.dumps({'schema':'noah-submitted-weights/1','day':doc['effective_day']}))
    elif fault=='unknown':(root/'day-0.submitted.json').write_text('{}')
    elif fault=='archive':(root/'points-policy-predecessor.json').write_bytes(b'wrong')
    else:(root/'points-policy.json').write_bytes(encoded(doc))
    with pytest.raises(ValueError):amendment.check_transition(adapter,doc)


def test_direct_route_and_current_weight_order(current):
    adapter,root,connections,client,marker=current
    call=client.extrinsics[2]['call']
    inner=next(arg['value'] for arg in call['call_args'] if arg['name']=='call')
    client.extrinsics[2]['call']=inner
    client.extrinsics[2]['address']=marker['hotkey']
    marker['route']='direct';marker['signer']=marker['hotkey']
    active=json.loads((root/'starter-active.json').read_bytes());active['marker']=marker
    (root/'starter-active.json').write_text(json.dumps(active))
    client.stored=list(reversed(client.stored))
    assert transition.confirm_legacy_current(adapter,root)['last_update']==101
