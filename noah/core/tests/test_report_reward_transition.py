"""One prospective boundary; original activation and publication proofs survive."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from noah.core.calculation_record import encoded
from noah.core.report_publication import ReportPublicationRefused
from noah.core.report_runtime import bind_activation, selected_reward
from noah.core import report_reward_transition as t
from noah.core.tests.test_report_publication import scenario


def boundary():
    request, *_ = scenario()
    config = request['policy']
    from noah.core.report_runtime import ELIGIBILITY
    config['eligibility_policy_version'] = ELIGIBILITY
    for contract in config['required_rounds']:
        contract['eligibility_policy_version'] = ELIGIBILITY
    activation = dict(schema='noah-report-activation/1',
                      **{k: config[k] for k in ('genesis_hash', 'mining_netuid', 'effective_day')})
    transition = dict(schema=t.SCHEMA, activation_sha256=t.digest(activation),
                      genesis_hash=activation['genesis_hash'], mining_netuid=82,
                      day_anchor_ts=request['day_anchor_ts'], effective_day=1,
                      from_reward='gate', to_reward='bonus-20',
                      announcement=dict(configuration=copy.deepcopy(config), sha256=t.digest(config)),
                      observed=dict(block_number=100, block_hash='ab'*32,
                                    timestamp_ms=request['day_anchor_ts']*1000))
    return activation, transition, config


def adapter(root, reward='bonus-20'):
    activation, transition, config = boundary()
    return SimpleNamespace(_submitted_record_dir=root, netuid=82, report_reward=reward,
                           day_anchor_ts=transition['day_anchor_ts'],
                           runner_chain=SimpleNamespace(genesis_hash=lambda: activation['genesis_hash']))


def install(root):
    activation, transition, config = boundary()
    (root/'report-state').mkdir()
    (root/t.ACTIVATION).write_bytes(encoded(activation))
    (root/t.PATH).write_bytes(encoded(transition))
    return activation, transition, config


def test_gate_bonus_boundary_keeps_original_and_allows_existing_schedule_rotation(tmp_path):
    activation, transition, config = install(tmp_path)
    before=(tmp_path/t.ACTIVATION).read_bytes()
    a=adapter(tmp_path)
    bind_activation(a,config,persist=True)
    assert selected_reward(a,0)=='gate'
    assert selected_reward(a,1)==selected_reward(a,100)=='bonus-20'
    # Existing boundary binding never froze the round list. This change must not.
    rotated=copy.deepcopy(config);rotated['required_rounds'][0]['round_date']='2099-01-01'
    bind_activation(a,rotated,persist=False)
    assert (tmp_path/t.ACTIVATION).read_bytes()==before
    with pytest.raises(ReportPublicationRefused,match='reward differs'):
        bind_activation(adapter(tmp_path,'gate'),config,persist=False)


@pytest.mark.parametrize('change',[{'from_reward':'bonus-20'},{'to_reward':'gate'},
    {'effective_day':0},{'effective_day':True},{'day_anchor_ts':1},
    {'activation_sha256':'cc'*32},{'genesis_hash':'cc'*32},{'mining_netuid':7},
    {'extra':True}])
def test_crossed_or_generalized_boundaries_refuse(change):
    activation, transition,_=boundary();transition.update(change)
    with pytest.raises(ReportPublicationRefused):t.validate_transition(transition,activation)


def test_nonprospective_boundary_and_calendar_refuse():
    activation, transition,_=boundary()
    transition['observed']['timestamp_ms']=(transition['day_anchor_ts']+86400)*1000
    with pytest.raises(ReportPublicationRefused,match='prospective'):t.validate_transition(transition,activation)


def test_transition_changed_or_removed_during_process_refuses(tmp_path):
    _,transition,config=install(tmp_path);a=adapter(tmp_path);bind_activation(a,config,persist=False)
    transition['effective_day']=2;(tmp_path/t.PATH).write_bytes(encoded(transition))
    with pytest.raises(ReportPublicationRefused,match='changed during'):bind_activation(a,config,persist=False)
    (tmp_path/t.PATH).unlink()
    with pytest.raises(ReportPublicationRefused,match='reward differs'):bind_activation(a,config,persist=False)


def test_native_gate_and_bonus_unchanged(tmp_path):
    _,_,config=boundary()
    for reward in ('gate','bonus-20'):
        root=tmp_path/reward;root.mkdir();a=adapter(root,reward)
        bind_activation(a,config,persist=False);assert not (root/t.ACTIVATION).exists()
        bind_activation(a,config,persist=True)
        assert selected_reward(a,1)==reward


def test_proof_reference_exact_and_only_after_boundary(tmp_path):
    _,transition,_=install(tmp_path)
    proof={'reward':'bonus-20','reward_transition':t.reference_for(transition,1),
           'request':dict(eval_day=1,genesis_hash=transition['genesis_hash'],mining_netuid=82,
                          day_anchor_ts=transition['day_anchor_ts'])}
    t.verify_proof_reference(tmp_path,proof)
    for changed in ({'eval_day':0},{'genesis_hash':'cd'*32},{'mining_netuid':7},{'day_anchor_ts':0}):
        bad=copy.deepcopy(proof);bad['request'].update(changed)
        with pytest.raises(ReportPublicationRefused):t.verify_proof_reference(tmp_path,bad)
    proof['reward_transition']['sha256']='ac'*32
    with pytest.raises(ReportPublicationRefused):t.verify_proof_reference(tmp_path,proof)


def test_boundary_alias_and_noncanonical_bytes_refused(tmp_path):
    _,_,config=install(tmp_path);path=tmp_path/t.PATH;raw=path.read_bytes();path.unlink()
    elsewhere=tmp_path/'elsewhere';elsewhere.write_bytes(raw);path.symlink_to(elsewhere)
    with pytest.raises(ReportPublicationRefused,match='link'):bind_activation(adapter(tmp_path),config,persist=False)
    path.unlink();path.write_bytes(raw+b' ')
    with pytest.raises(ReportPublicationRefused,match='canonical'):bind_activation(adapter(tmp_path),config,persist=False)


def next_day(root, request, result, source):
    """Retain a second complete synthetic R0/request, preserving day zero bytes."""
    from datetime import datetime, timezone
    from noah.core.calculation_record import artifact, retain, verify
    from noah.core.report_publication import semantic_digest
    manifest=verify(root,source)
    fixture=copy.deepcopy(artifact(root,manifest,'fixture'));fixture['evalDay']='1'
    score=copy.deepcopy(artifact(root,manifest,'agreed_score'));score['evalDay']='1'
    for row in score['miners']:
        row.update(doorOpen=True,state={'maturity':1000000,'missed':0})
    context=copy.deepcopy(artifact(root,manifest,'context'));context.update(day=1,height=200,
        day_start_utc_unix=request['day_anchor_ts']+86400)
    new=retain(root,fixture=fixture,agreed_text=encoded(score).decode(),vector={5:65535,6:0},context=context)
    request=copy.deepcopy(request);request['eval_day']=1
    contract=request['policy']['required_rounds'][0]
    from noah.core.report_runtime import ELIGIBILITY
    request['policy']['eligibility_policy_version']=ELIGIBILITY;contract['eligibility_policy_version']=ELIGIBILITY
    for key in contract:
        if key.endswith('_ms'):contract[key]+=86400000
    contract['round_date']=datetime.fromtimestamp(contract['block_timestamp_ms']/1000,timezone.utc).date().isoformat()
    request['policy_sha256']=semantic_digest(request['policy'])
    request['evaluation']['timestamp_ms']=contract['reveal_close_ms']
    request['calculation']={'reference':new,'fixture_sha256':verify(root,new)['artifacts']['fixture']['sha256']}
    result=copy.deepcopy(result);result.update(request_sha256=semantic_digest(request),policy_sha256=request['policy_sha256'])
    sha=semantic_digest(contract)
    for seat in result['scored_decisions']:
        for row in seat['rounds']:row['round_sha256']=sha
    result['rounds'][0].update(round_sha256=sha,evaluation=request['evaluation'])
    return new,request,result


def test_mixed_historical_gate_and_new_bonus_full_replay(tmp_path):
    from noah.core.tests.test_report_publication import stored
    from noah.core.report_publication import retain_report, verify_report
    request,result,_,submission,source,package=stored(tmp_path)
    old,oldvector=retain_report(tmp_path,source,request,package,submission,replay=lambda *_:result)
    _,transition,_=install(tmp_path)
    before={p:p.read_bytes()for p in tmp_path.rglob('*')if p.is_file()}
    new,req,res=next_day(tmp_path,request,result,source)
    fresh,vector=retain_report(tmp_path,new,req,package,submission,replay=lambda *_:res,
                              reward='bonus-20',reward_transition=t.reference_for(transition,1))
    assert oldvector[5]==0 and vector[5]>0
    assert verify_report(tmp_path,old,replay=lambda *_:result).get('reward','gate')=='gate'
    assert verify_report(tmp_path,fresh,replay=lambda *_:res)['reward']=='bonus-20'
    assert all(p.read_bytes()==raw for p,raw in before.items())
    (tmp_path/t.PATH).unlink()
    with pytest.raises(ReportPublicationRefused):verify_report(tmp_path,fresh,replay=lambda *_:res)


def test_old_refresh_under_bonus_configuration_does_not_rebonus(tmp_path,monkeypatch):
    import json
    from noah.core.tests.test_report_toggle_recovery import _ready
    from noah.core.report_runtime import check_before_send
    from noah.core.calculation_record import verify
    a,client,source,bound,vector,ordinary,_,policy_path=_ready(tmp_path,monkeypatch,'legacy',False)
    config=json.loads(policy_path.read_text());bind_activation(a,config,persist=True)
    activation=json.loads((a._submitted_record_dir/t.ACTIVATION).read_bytes())
    transition=dict(schema=t.SCHEMA,activation_sha256=t.digest(activation),
        genesis_hash=activation['genesis_hash'],mining_netuid=82,day_anchor_ts=a.day_anchor_ts,
        effective_day=source['day']+1,from_reward='gate',to_reward='bonus-20',
        announcement={'configuration':config,'sha256':t.digest(config)},
        observed={'block_number':100,'block_hash':'ab'*32,
                  'timestamp_ms':(a.day_anchor_ts+source['day']*86400)*1000})
    (a._submitted_record_dir/t.PATH).write_bytes(encoded(transition))
    # Restart semantics: a live process would correctly refuse adding a boundary.
    del a._report_transition_digest
    a.report_reward='bonus-20'
    check_before_send(a,bound,vector)
    a.publish_direct(vector,99,refresh_epoch=200,source_day=source['day'])
    assert len(client.calls)==1
    assert {int(k):v for k,v in verify(a._submitted_record_dir,bound)['publishable_weights_u16'].items()}==vector
    from noah.core.calculation_record import artifact
    proof=artifact(a._submitted_record_dir,verify(a._submitted_record_dir,bound),'context')['report_gate']
    assert proof.get('reward','gate')=='gate' and 'reward_transition' not in proof
    assert vector[4]==0  # preserved gate outcome, never a new bonus calculation.


def test_transition_does_not_reprice_gate_intent_after_lost_reply(tmp_path,monkeypatch):
    import json
    from noah.core.tests.test_report_toggle_recovery import _ready, PreparedTransportFake
    from noah.core.publish import NotOnChain
    a,client,source,bound,vector,*_= _ready(tmp_path,monkeypatch,'legacy',False)
    client.lost_reply=True
    with pytest.raises(NotOnChain):a.publish_direct(vector,99)
    original=copy.deepcopy(a._publish_intent.load())
    activation=t.read_document(a._submitted_record_dir/t.ACTIVATION)
    config=json.loads(Path(a.report_gate_policy_path).read_text())
    transition=dict(schema=t.SCHEMA,activation_sha256=t.digest(activation),
        genesis_hash=activation['genesis_hash'],mining_netuid=82,day_anchor_ts=a.day_anchor_ts,
        effective_day=source['day']+1,from_reward='gate',to_reward='bonus-20',
        announcement={'configuration':config,'sha256':t.digest(config)},
        observed={'block_number':100,'block_hash':'ab'*32,
                  'timestamp_ms':(a.day_anchor_ts+source['day']*86400)*1000})
    # This is adversarial historical replay, not permission for the owner command
    # to create a boundary while an intent exists (the owner must refuse that).
    (a._submitted_record_dir/t.PATH).write_bytes(encoded(transition))
    a.report_reward='bonus-20';a._calculation_reference=None;client.lost_reply=False
    with monkeypatch.context() as patch:
        patch.setattr(PreparedTransportFake,'prepare',lambda *_:pytest.fail('duplicate signing'))
        patch.setattr(PreparedTransportFake,'send',lambda *_:pytest.fail('duplicate send'))
        assert a._already_published_direct(99)
    saved=json.loads((a._submitted_record_dir/f"day-{source['day']}.submitted.json").read_bytes())
    assert saved['direct_intent']['intent_id']==original['intent_id']
    assert saved['submitted_weights_u16']==original['vector']
    assert a._publish_intent.load() is None and len(client.calls)==1


def test_new_day_gate_proof_refuses_bonus_boundary_and_bonus_sends(tmp_path,monkeypatch):
    from noah.core.tests.test_report_publication import stored, _sending
    from noah.core.report_publication import retain_report
    from noah.core.report_runtime import check_before_send
    request,result,_,submission,source,package=stored(tmp_path)
    _,transition,_=install(tmp_path)
    new,req,res=next_day(tmp_path,request,result,source)
    gate,gv=retain_report(tmp_path,new,req,package,submission,replay=lambda *_:res)
    bonus,bv=retain_report(tmp_path,new,req,package,submission,replay=lambda *_:res,
                          reward='bonus-20',reward_transition=t.reference_for(transition,1))
    monkeypatch.setattr('noah.core.report_publication.replay_package',lambda *_:res)
    monkeypatch.setattr('noah.core.report_runtime.load_policy',lambda _:req['policy'])
    a=_sending(tmp_path,req,submission);a.report_reward='bonus-20'
    with pytest.raises(ReportPublicationRefused,match='reward differs'):check_before_send(a,gate,gv)
    check_before_send(a,bonus,bv)
    # Policy rotation remains its existing contract: old proof must not silently
    # authorize a send under a different required round; offline replay is separate.
    rotated=copy.deepcopy(req['policy']);rotated['required_rounds'][0]['scoring_release']='other'
    monkeypatch.setattr('noah.core.report_runtime.load_policy',lambda _:rotated)
    with pytest.raises(ReportPublicationRefused,match='policy differs'):check_before_send(a,bonus,bv)


def test_boundary_hardlink_and_oversize_refuse(tmp_path):
    import os
    _,_,config=install(tmp_path)
    path=tmp_path/t.PATH
    os.link(path,tmp_path/'second-name')
    with pytest.raises(ReportPublicationRefused,match='file refused'):
        bind_activation(adapter(tmp_path),config,persist=False)
    (tmp_path/'second-name').unlink()
    path.write_bytes(b' '* (t.MAX_BYTES+1))
    with pytest.raises(ReportPublicationRefused,match='file refused'):
        bind_activation(adapter(tmp_path),config,persist=False)


def test_oversize_announcement_refuses_before_write():
    activation,transition,_=boundary()
    transition['announcement']['configuration']['oversize']='x'*t.MAX_BYTES
    with pytest.raises(ReportPublicationRefused,match='byte bound'):
        t.validate_transition(transition,activation)
