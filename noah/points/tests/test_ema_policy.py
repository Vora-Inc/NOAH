"""Document-only boundary tests: no on-chain EMA field or transition acceptance."""
import copy
import hashlib
import json

import pytest

from noah.core.calculation_record import encoded
from noah.points import amendment, ema_policy
from noah.points.tests.test_integration import captured


@pytest.fixture
def previous(tmp_path):
    _, old = captured(tmp_path)
    return encoded(amendment.build(encoded(old)))


def anchor(previous):
    cutoff = amendment.validate_document(json.loads(previous))['migration']['cutoff_block']
    return {'block': cutoff + 100, 'hash': '0x' + 'a' * 64}


def test_exact_nested_predecessors_and_all_financial_sections_preserved(previous):
    current = ema_policy.build(previous, anchor(previous))
    assert current['predecessor_json'].encode() == previous
    assert current['predecessor_sha256'] == hashlib.sha256(previous).hexdigest()
    assert ema_policy.baseline(current) == amendment.validate_document(json.loads(previous))
    assert ema_policy.manifest(current, 2)['policy'] == ema_policy.POLICY
    assert ema_policy.fixture_boundary(current, 2)['effectiveBlock'] == anchor(previous)['block'] + 1


def test_preserves_noncanonical_predecessor_bytes(previous):
    raw = json.dumps(json.loads(previous), indent=1).encode() + b'\n\n'
    assert ema_policy.build(raw, anchor(previous))['predecessor_json'].encode() == raw


def test_complete_block_rule_is_independent_of_discovery_or_restart(previous):
    doc = ema_policy.build(previous, anchor(previous))
    boundary = doc['effective_block']
    for current in (doc, json.loads(encoded(doc))):
        for block in (0, boundary-1):
            assert not ema_policy.uses_ema(current, block)
        for block in (boundary, boundary+100):
            assert ema_policy.uses_ema(current, block)


@pytest.mark.parametrize('field,value', [
    ('policy', amendment.POLICY), ('schema', amendment.SCHEMA), ('rule', amendment.RULE),
    ('effective_block', True), ('effective_block', 0),
    ('predecessor_sha256', 'f'*64), ('predecessor_json', '{}'),
    ('activation_anchor', {'block': True, 'hash':'0x'+'a'*64}),
    ('activation_anchor', {'block': 0, 'hash':'0x'+'a'*64}),
    ('activation_anchor', {'block': 2**53, 'hash':'0x'+'a'*64}),
    ('activation_anchor', {'block': 500000, 'hash':'invalid'}),
])
def test_changed_or_invalid_envelope_refuses(previous, field, value):
    doc=ema_policy.build(previous, anchor(previous));doc[field]=value
    with pytest.raises(ValueError):ema_policy.validate_document(doc)


def test_cannot_relabel_old_v2_as_v4_predecessor(previous):
    old = json.loads(previous)['predecessor_json'].encode()
    with pytest.raises(ValueError):ema_policy.build(old, anchor(previous))


def test_unknown_fields_and_duplicate_json_keys_refuse(previous):
    doc=ema_policy.build(previous, anchor(previous));doc['extra']=1
    with pytest.raises(ValueError):ema_policy.validate_document(doc)
    text=previous.decode().rstrip()[:-1] + ',"policy":"' + amendment.POLICY + '"}'
    with pytest.raises(ValueError):ema_policy.build(text.encode(),anchor(previous))


def test_normal_runtime_preserves_nested_financial_state_and_requires_both_archives(tmp_path):
    from noah.points.runtime import RuntimePolicy
    adapter,old=captured(tmp_path)
    original=encoded(old);prior=encoded(amendment.build(original))
    at=old['migration']['cutoff_block']+2
    doc=ema_policy.build(prior,{'block':at,'hash':old['migration']['cutoff_hash']})
    adapter.runner_chain.head_block=lambda:at+1
    (tmp_path/'points-policy.json').write_bytes(encoded(doc))
    (tmp_path/'points-policy-predecessor.json').write_bytes(original)
    with pytest.raises(FileNotFoundError):RuntimePolicy(doc,adapter)
    (tmp_path/'points-policy-ema-predecessor.json').write_bytes(prior)
    policy=RuntimePolicy(doc,adapter)
    assert policy.migration==old['migration']
    assert policy.manifest==ema_policy.manifest(doc,adapter.netuid)
    assert policy.input_schema==ema_policy.INPUT_SCHEMA
    assert len(policy.baselines)>0 and policy.migration['consumed_receipt_ids']==old['migration']['consumed_receipt_ids']
    changed=copy.deepcopy(doc);changed['activation_anchor']['block']+=1;changed['effective_block']+=1
    with pytest.raises(ValueError,match='existing points policy differs'):RuntimePolicy(changed,adapter)


def transition_state(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from noah.points import transition
    _,old=captured(tmp_path)
    prior=encoded(amendment.build(encoded(old)));at=anchor(prior)
    doc=ema_policy.build(prior,at)
    root=tmp_path/'runtime/ticket-16/scores';root.mkdir(parents=True)
    (root.parent.parent/'points-policy-amendment.json').write_bytes(prior)
    (root.parent.parent/'points-policy.json').write_bytes(encoded(old))
    (root/'points-policy-predecessor.json').write_bytes(encoded(old))
    (root/'points-policy.json').write_bytes(prior)
    adapter=SimpleNamespace(_submitted_record_dir=root,_publish_intent=SimpleNamespace(load=lambda:None),
        _commit_ledger=SimpleNamespace(load=lambda:{}),runner_chain=SimpleNamespace(head_block=lambda:at['block']+1,block_hash=lambda _:at['hash']))
    calls=[]
    monkeypatch.setattr(transition,'confirm_current',lambda *args,**kw:calls.append(kw))
    return root,adapter,doc,calls


def test_transition_initial_and_archive_first_resume_never_mutate_baselines(tmp_path,monkeypatch):
    root,adapter,doc,calls=transition_state(tmp_path,monkeypatch)
    before={p.name:p.read_bytes() for p in root.iterdir()}
    assert ema_policy.check_transition(adapter,doc)==json.loads(doc['predecessor_json'])
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}
    (root/'points-policy-ema-predecessor.json').write_bytes(doc['predecessor_json'].encode())
    assert ema_policy.check_transition(adapter,doc)
    (root/'points-policy.json').write_bytes(encoded(doc))
    assert ema_policy.check_transition(adapter,doc)
    assert len(calls)==3 and all(c['allowed_modes']==('legacy',amendment.MODE) for c in calls)


@pytest.mark.parametrize('location',['foreign_valuations','receiptDispositions','pointsReceipts'])
def test_retained_new_boundary_receipt_cannot_be_repriced(tmp_path,monkeypatch,location):
    root,adapter,doc,calls=transition_state(tmp_path,monkeypatch)
    path=root/'retained.json'
    path.write_text(json.dumps({location:[{'block':doc['effective_block']-1}]}))
    assert ema_policy.check_transition(adapter,doc)
    path.write_text(json.dumps({location:[{'block':doc['effective_block']}]}))
    with pytest.raises(ValueError,match='forbids repricing'):ema_policy.check_transition(adapter,doc)


@pytest.mark.parametrize('mutation',['pending','archive','configured','anchor','fast'])
def test_transition_mutations_refuse(tmp_path,monkeypatch,mutation):
    root,adapter,doc,_=transition_state(tmp_path,monkeypatch)
    if mutation=='pending':adapter._publish_intent.load=lambda:{'pending':True}
    elif mutation=='archive':(root/'points-policy-predecessor.json').write_text('{}')
    elif mutation=='configured':(root.parent.parent/'points-policy-amendment.json').write_text('{}')
    elif mutation=='anchor':adapter.runner_chain.block_hash=lambda _:'0x'+'f'*64
    else:doc['ema_source']='SubnetFastMovingPrice'
    with pytest.raises(ValueError):ema_policy.check_transition(adapter,doc)
