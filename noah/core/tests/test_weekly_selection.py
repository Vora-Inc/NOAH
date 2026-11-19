"""One paired raw boundary; exact target rounds and bounded financial contexts."""
import copy

import pytest

from noah.core import report_runtime as runtime, report_publication
from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.tests import test_weekly_runtime as cases

prepared = cases.prepared
unsigned_week = cases.unsigned_week
from noah.core.weekly_runtime import bind_template, expand_resolution
from noah.reports.assignments import build_week, sign_week, week_start_date, schedule_for_target
from noah.reports.common import digest
from noah.reports.schedule import _resolve
from noah.reports.tests.test_schedule import Chain, ANCHOR


def signed_case(p, monkeypatch):
    signed=sign_week(unsigned_week(p),p.pair,trusted_public_key=p.configuration['assignment_signer_public_key'])
    calls=[]
    chain=Chain(shift=p.a.day_anchor_ts-ANCHOR,head=16000)
    def resolve(configuration,endpoints,day):
        calls.append(configuration['reported_netuid'])
        return _resolve(configuration,[chain,chain],day)
    monkeypatch.setattr(runtime,'resolve_schedule',resolve)
    policy,selection=runtime.select_weekly(p.configuration,signed,[],p.day,p.a.day_anchor_ts)
    request=dict(eval_day=p.day,day_anchor_ts=p.a.day_anchor_ts,assignments=signed['assignments'],
                 policy=policy,policy_sha256=digest(policy))
    return signed,chain,calls,selection,request


def test_one_reader_pair_reconstructs_identical_target_round_ids(prepared,monkeypatch):
    p=prepared
    signed,chain,calls,selection,request=signed_case(p,monkeypatch)
    targets=sorted(signed['assignments'][0]['targets'])
    assert calls==[targets[0]]
    expected=[_resolve(schedule_for_target(p.configuration,n),[chain,chain],p.day)['round'] for n in targets]
    assert request['policy']['required_rounds']==expected
    runtime.verify_selection(selection,request)
    assert [digest(r) for r in expected]==[digest(r) for r in request['policy']['required_rounds']]


@pytest.mark.parametrize('kind',['target','prefix','window','configuration','extra'])
def test_rehashed_compact_selection_cannot_cross_target_configuration_or_window(prepared,monkeypatch,kind):
    p=prepared
    _,_,_,selection,request=signed_case(p,monkeypatch)
    changed=copy.deepcopy(selection);base=changed['resolution']['base']
    if kind=='target':base['round']['reported_netuid']=123
    elif kind=='prefix':base['round']['release_prefix']='ff'*16
    elif kind=='window':base['boundary_proof']['target_ms']+=1
    elif kind=='configuration':
        changed['configuration']['snapshot_offset_seconds']+=1
        changed['configuration_sha256']=digest(changed['configuration'])
    else:changed['resolution']['rounds']=[]
    with pytest.raises((ValueError, report_publication.ReportPublicationRefused)):runtime.verify_selection(changed,request)


def test_full_214_seat_128_target_r1_context_and_final_send_fit_four_mib(prepared,monkeypatch):
    """Real twins/R0/R1/final guard, raw boundary replay; provider IPC is synthetic."""
    from noah.chain.ss58 import address_of,public_key_of
    from noah.points import onboarding
    from noah.points.tests.test_no_pool import economics_fixture
    from noah.points.run import agreed
    from noah.points.publication import check_before_send
    p=prepared
    p.a.day_anchor_ts=ANCHOR
    p.configuration['day_anchor_ts']=ANCHOR
    from pathlib import Path
    Path(p.a.report_gate_policy_path).write_bytes(encoded(p.configuration))
    fixture=economics_fixture([[(100*10**9,0,0),(0,75*10**9,0)]]*214)
    fixture['dayEpochTs']=str(ANCHOR)
    hotkeys={str(i):address_of((1000+i).to_bytes(32,'little')) for i in range(1,215)}
    coldkeys={str(i):address_of((2000+i).to_bytes(32,'little')) for i in range(1,215)}
    for i,miner in enumerate(fixture['miners'],1):miner.update(uid=i,coldkey=coldkeys[str(i)])
    fixture.update(pointsHotkeys=hotkeys,pointsOwners=coldkeys,pointsRules=onboarding.RULES,pointsExcludedUids=[0])
    score=agreed(fixture);vector={int(k):v for k,v in score['weightsU16'].items()}
    context=artifact(p.root,verify(p.root,p.raw),'context')
    p.root=p.root.parent/'large-scores'
    p.a._submitted_record_dir=p.root
    runtime.bind_activation(p.a,p.configuration,persist=True)
    context.update(scored_hotkeys=hotkeys,scored_coldkeys=coldkeys,day_start_utc_unix=ANCHOR+p.day*86400)
    raw=retain(p.root,fixture=fixture,agreed_text=encoded(score).decode(),vector=vector,context=context)
    p.a._calculation_reference=raw
    roster=[dict(uid=i,hotkey=public_key_of(hotkeys[str(i)]).hex(),coldkey=public_key_of(coldkeys[str(i)]).hex(),
                 registration_block=1) for i in range(1,215)]
    week=sign_week(build_week(p.configuration,roster,list(range(1,129)),
        week_start=week_start_date(p.a.day_anchor_ts+p.day*86400).isoformat(),roster_block_number=1,
        roster_block_hash='01'*32),p.pair,trusted_public_key=p.configuration['assignment_signer_public_key'])
    bind_template(p.root,p.configuration,persist=True)
    calls=[]
    chain=Chain(head=10003000,shift=p.a.day_anchor_ts-ANCHOR+(7250-9995000)*12)
    def resolve(config,endpoints,day):
        calls.append(config['reported_netuid']);return _resolve(config,[chain,chain],day)
    monkeypatch.setattr(runtime,'resolve_schedule',resolve)
    policy,selection=runtime.select_weekly(p.configuration,week,[],p.day,p.a.day_anchor_ts)
    assert calls==[1] and len(policy['required_rounds'])==128
    p.a.runner_chain.head_block=lambda:10003000
    p.a.runner_chain.block_hash=chain.block_hash
    p.a.runner_chain.block_timestamp=lambda n:p.a.day_anchor_ts+(183000 if n==10003000 else 12)
    p.a.runner_chain.block_timestamp_ms=lambda n:p.a.runner_chain.block_timestamp(n)*1000
    p.a.runner_chain.registration_blocks=lambda n:{i:1 for i in range(1,215)}
    p.a.runner_chain.seat_hotkeys=lambda n:{int(k):v for k,v in hotkeys.items()}
    p.a.runner_chain.hotkey_owners=lambda keys,n:{hotkeys[k]:v for k,v in coldkeys.items()}
    request=runtime.request_for(p.a,raw,policy,week['assignments'])
    contracts=policy['required_rounds'];round_shas=[digest(c) for c in contracts]
    assigned={r['uid']:set(r['targets']) for r in week['assignments']}
    decisions=[]
    for seat in request['scored_seats']:
        rounds=[]
        for contract,sha in zip(contracts,round_shas):
            required=contract['reported_netuid'] in assigned[seat['uid']]
            rounds.append(dict(round_sha256=sha,effect='preserve',outcome='verified' if required else 'not_required',
                               status='VERIFIED' if required else None,reason='exact_match' if required else 'not_assigned'))
        decisions.append({**{k:seat[k] for k in ('uid','hotkey','coldkey','registration_block')},
                          'effect':'preserve','outcome':'verified','rounds':rounds})
    result=dict(schema='noah.report-adjudication-result/1',manifest_sha256='cc'*32,request_sha256=digest(request),
        policy_sha256=request['policy_sha256'],status='COMPLETE',scored_decisions=decisions,
        rounds=[dict(round_sha256=sha,required_roster_sha256='dd'*32,evaluation=request['evaluation'],
                readers=dict(reader_a='ee'*32,reader_b='ff'*32),engine_sha256='01'*32,calculation_sha256='02'*32,
                verdicts_sha256='03'*32,resolved=True) for sha in round_shas])
    def replay(directory,package,frozen):
        assert frozen==request and package['sha256']==result['manifest_sha256'];return copy.deepcopy(result)
    monkeypatch.setattr(report_publication,'replay_package',replay)
    package=dict(path='reports/'+'cc'*32+'/manifest.json',sha256='cc'*32)
    submission=dict(live_uids=list(range(1,215)),owner_uids=[0],fixture_hotkeys=hotkeys,chain_hotkeys=hotkeys)
    ref,final=report_publication.retain_report(p.root,raw,request,package,submission,selection=selection,reward='bonus-20')
    check_before_send(p.a,ref,final)
    final_context=artifact(p.root,verify(p.root,ref),'context')
    current_bytes=len(encoded(final_context))
    assert current_bytes<4*1024**2
    old_context=copy.deepcopy(final_context)
    old_context['report_gate']['selection']['resolution']=dict(schema='noah.weekly-resolution/1',week=week,
        rounds=expand_resolution(p.configuration,week,p.day,p.a.day_anchor_ts,selection['resolution']['base']))
    old_bytes=len(encoded(old_context))
    assert old_bytes>4*1024**2  # Reproduces the real retention-cap defect, not merely an estimate.
    measurement=dict(schema='noah.synthetic-weekly-context-capacity/1',targets=128,seats=214,head=10003000,
        compact_context_bytes=current_bytes,historical_expanded_context_bytes=old_bytes,
        existing_context_limit=4*1024**2,actual_final_send_check=True,provider_ipc='synthetic',raw_boundary_replay=True)
    (p.root.parent/'weekly-context-capacity.json').write_bytes(encoded(measurement))
    assert verify(p.root,ref)['artifacts']['agreed_score']==verify(p.root,raw)['artifacts']['agreed_score']
