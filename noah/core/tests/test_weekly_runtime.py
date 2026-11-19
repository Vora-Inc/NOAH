"""Real retained V2 graphs; optional weekly reporting never replaces raw pay."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import sr25519

from noah.chain.ss58 import public_key_of, address_of
from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.report_publication import ReportPublicationRefused
from noah.core import report_runtime as runtime
from noah.core.weekly_runtime import (AUTHORITY, bind_template, marker_path, signing_pair,
    retain_no_bonus, verify_no_bonus, validate_no_bonus_marker)
from noah.points.tests.test_no_pool import ordinary_published
from noah.reports.tests.test_schedule import schedule


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from noah.core.tests import test_direct_runtime475
    original = test_direct_runtime475.adapter
    def with_public_identities(*args, **kwargs):
        a, client = original(*args, **kwargs)
        a.ark_coldkey = address_of(bytes.fromhex('55'*32))
        a.designated_validator_hotkey = address_of(bytes.fromhex('66'*32))
        return a, client
    monkeypatch.setattr(test_direct_runtime475, 'adapter', with_public_identities)
    a, client, fixture, vector, day = ordinary_published(tmp_path, genesis='0x'+'11'*32)
    root = Path(a._submitted_record_dir)
    raw = a._calculation_reference
    context = artifact(root, verify(root, raw), 'context')
    configuration = schedule()
    configuration.pop('reported_netuid')
    pair = sr25519.pair_from_seed(bytes([42])*32)
    configuration.update(schema='noah.weekly-assignment-template/2', effective_day=0,
        genesis_hash='11'*32, mining_netuid=a.netuid, day_anchor_ts=a.day_anchor_ts,
        ark_coldkey=public_key_of(context['ark_address']).hex(),
        ark_validator_hotkey=public_key_of(context['designated_validator_hotkey']).hex(),
        assignment_signer_public_key=pair[0].hex(), commitment_format='context-v1')
    policy = tmp_path/'weekly-policy.json'; policy.write_bytes(encoded(configuration))
    a.report_gate_enabled = True
    a.weekly_assignments = True
    a.report_reward = 'bonus-20'
    a.report_gate_policy_path = str(policy)
    runtime.bind_activation(a, configuration, persist=True)
    return SimpleNamespace(a=a, client=client, root=root, raw=raw, vector=vector,
        day=day, configuration=configuration, pair=pair, fixture=fixture)


def test_missing_week_retains_exact_raw_graph_and_passes_real_final_report_check(prepared):
    p=prepared
    before={f:f.read_bytes() for f in p.root.rglob('*.json')}
    assert runtime.prepare_daily(p.a,p.vector)==p.vector
    ref=p.a._calculation_reference
    assert ref!=p.raw
    proof=verify_no_bonus(p.root,ref)
    assert proof['reason']=='assignment_unavailable' and proof['source']==p.raw
    raw=verify(p.root,p.raw); final=verify(p.root,ref)
    assert raw['publishable_weights_u16']==final['publishable_weights_u16']
    assert raw['artifacts']['fixture']==final['artifacts']['fixture']
    assert raw['artifacts']['agreed_score']==final['artifacts']['agreed_score']
    assert all(f.read_bytes()==b for f,b in before.items())
    runtime.check_before_send(p.a,ref,p.vector)
    from noah.points.publication import check_before_send
    check_before_send(p.a,ref,p.vector)  # unchanged twin/mode/current identity/expiry checks also execute
    marker=json.loads(marker_path(p.root,p.day).read_bytes())
    assert validate_no_bonus_marker(p.root,marker)==proof


def test_late_week_cannot_upgrade_frozen_no_bonus_day(prepared,monkeypatch):
    p=prepared
    runtime.prepare_daily(p.a,p.vector); ref=p.a._calculation_reference
    p.a._calculation_reference=p.raw
    monkeypatch.setattr(runtime,'_retained_week',lambda *a: (_ for _ in ()).throw(AssertionError('late week consulted')))
    assert runtime.prepare_daily(p.a,p.vector)==p.vector and p.a._calculation_reference==ref


def test_frozen_request_cannot_be_discarded_for_missing_assignments(prepared):
    p=prepared
    (p.root/'report-state'/f'day-{p.day}.request.json').write_bytes(b'{}')
    with pytest.raises(ReportPublicationRefused,match='frozen report request'):
        runtime.prepare_daily(p.a,p.vector)
    assert not marker_path(p.root,p.day).exists()


@pytest.mark.parametrize('kind',['vector','score','configuration','source','transition'])
def test_no_bonus_tampering_cannot_mint_changed_pay(prepared,kind):
    p=prepared
    ref=retain_no_bonus(p.root,p.raw,p.configuration,reason='assignment_unavailable')
    manifest=verify(p.root,ref);context=artifact(p.root,manifest,'context')
    score=artifact(p.root,manifest,'agreed_score'); vector=dict(p.vector)
    if kind=='vector':vector[3]-=1
    elif kind=='score':score['miners'][0]['payWeightFp']='1'
    elif kind=='configuration':context['report_no_bonus']['configuration_sha256']='00'*32
    elif kind=='source':context['report_no_bonus']['source']=ref
    else:context['report_no_bonus']['reward_transition']={'path':'bad','sha256':'00'*32}
    forged=retain(p.root,fixture=artifact(p.root,manifest,'fixture'),agreed_text=encoded(score).decode(),
                  vector=vector,context=context,canonical_day=False)
    with pytest.raises(ReportPublicationRefused):verify_no_bonus(p.root,forged)


def test_no_bonus_off_blocks_new_send_without_changing_retained_record(prepared):
    p=prepared
    runtime.prepare_daily(p.a,p.vector);ref=p.a._calculation_reference
    p.a.report_gate_enabled=False
    with pytest.raises(ReportPublicationRefused,match='selected weekly'):
        runtime.check_before_send(p.a,ref,p.vector)
    assert verify_no_bonus(p.root,ref)


def test_weekly_authority_cannot_be_replaced_or_removed(prepared):
    p=prepared
    bind_template(p.root,p.configuration,persist=True)
    changed={**p.configuration,'assignment_signer_public_key':'00'*32}
    with pytest.raises(ReportPublicationRefused,match='authority differs'):
        bind_template(p.root,changed)
    runtime.prepare_daily(p.a,p.vector)
    (p.root/AUTHORITY).unlink()
    with pytest.raises(ReportPublicationRefused,match='lacks retained authority'):
        bind_template(p.root,p.configuration,persist=True)


def test_seed_loader_is_separate_private_native_key_and_refuses_aliases(tmp_path):
    seed = bytes([42] * 32)
    pair=sr25519.pair_from_seed(seed)
    path=tmp_path/'assignment.seed';path.write_bytes(seed);path.chmod(0o600)
    assert signing_pair(path,pair[0].hex())==pair
    with pytest.raises(ValueError):
        signing_pair(path,'00'*32)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        signing_pair(path,pair[0].hex())
    path.chmod(0o600);alias=tmp_path/'alias';alias.symlink_to(path)
    with pytest.raises(ValueError):
        signing_pair(alias,pair[0].hex())


def test_off_and_read_only_follower_never_open_assignment_key(monkeypatch):
    from noah.core import weekly_runtime
    monkeypatch.setattr(weekly_runtime,'signing_pair',lambda *a: pytest.fail('unused key read'))
    assert runtime.publish_weekly(SimpleNamespace(report_gate_enabled=False,weekly_assignments=True)) is None
    assert runtime.publish_weekly(SimpleNamespace(report_gate_enabled=True,weekly_assignments=False)) is None
    assert runtime.publish_weekly(SimpleNamespace(report_gate_enabled=True,weekly_assignments=True)) is None


def test_old_gate_day_uses_original_transition_announcement():
    original=schedule();new={**original,'schema':'noah.weekly-assignment-template/2'}
    a=SimpleNamespace(_report_reward_transition={'effective_day':59,'announcement':{'configuration':original}})
    assert runtime.configuration_for_day(a,new,58)==original
    assert runtime.configuration_for_day(a,new,59)==new


def unsigned_week(p, *, targets=(4, 8, 9)):
    from noah.reports.assignments import build_week, week_start_date
    context = artifact(p.root, verify(p.root, p.raw), 'context')
    roster = [dict(uid=3, hotkey=public_key_of(context['scored_hotkeys']['3']).hex(),
                   coldkey=public_key_of(context['scored_coldkeys']['3']).hex(), registration_block=1)]
    return build_week(p.configuration, roster, list(targets),
        week_start=week_start_date(p.a.day_anchor_ts + p.day*86400).isoformat(),
        roster_block_number=1, roster_block_hash='01'*32)


@pytest.mark.parametrize('drift', [0, 300001, -1, 7*86400000])
def test_publisher_binds_unsigned_worker_to_current_clock_before_dedicated_signing(prepared, monkeypatch, drift):
    p=prepared
    timestamp=(p.a.day_anchor_ts+p.day*86400)*1000
    p.a.runner_chain.block_timestamp_ms=lambda height: timestamp+drift if height==101 else timestamp
    p.a.runner_chain.head_block=lambda:101
    p.a.runner_chain.block_hash=lambda height:'0x'+'ab'*32
    key = p.root.parent.joinpath('assignment.seed')
    key.write_bytes(bytes([42])*32)
    key.chmod(0o600)
    p.a.weekly_assignment_signing_key_file=str(key)
    body=unsigned_week(p)
    def worker(action,*arguments):
        assert action=='assignment-week'
        invocation=json.loads(Path(arguments[1]).read_bytes())
        assert set(invocation)=={'schema','template','endpoint'}
        assert invocation['template']==p.configuration
        return dict(schema='noah.weekly-assignment-build-result/1',week=body,
                    finalized=dict(block_number=100,block_hash='ab'*32,timestamp_ms=timestamp))
    monkeypatch.setattr(runtime,'run_worker',worker)
    if drift:
        from noah.core import weekly_runtime
        monkeypatch.setattr(weekly_runtime,'signing_pair',lambda *a:pytest.fail('stale/future worker signed'))
    result=runtime.publish_weekly(p.a)
    if drift:
        assert result is None
        assert not (p.root/'report-state/weekly-assignments').exists()
    else:
        from noah.core.weekly_runtime import verify_week_for
        assert verify_week_for(p.configuration,result,p.day,p.a.day_anchor_ts)==result
        assert runtime._retained_week(p.root,p.day,p.a.day_anchor_ts,p.configuration)==result
        assert result['assignment_signer_public_key']!=p.configuration['ark_validator_hotkey']


def test_no_targets_is_retained_authenticated_no_bonus(prepared):
    from noah.reports.assignments import sign_week, retain_week
    p=prepared
    bind_template(p.root,p.configuration,persist=True)
    signed=sign_week(unsigned_week(p,targets=()),p.pair,
                     trusted_public_key=p.configuration['assignment_signer_public_key'])
    retain_week(p.root/'report-state/weekly-assignments',signed,
                trusted_public_key=p.configuration['assignment_signer_public_key'])
    assert runtime.prepare_daily(p.a,p.vector)==p.vector
    proof=verify_no_bonus(p.root,p.a._calculation_reference)
    assert proof['reason']=='no_assigned_targets' and proof['week']==signed
    runtime.check_before_send(p.a,p.a._calculation_reference,p.vector)


def test_retained_weekly_history_without_activation_cannot_invent_boundary(prepared):
    p=prepared
    runtime.prepare_daily(p.a,p.vector)
    activation=p.root/'report-state/activation.json'
    activation.unlink()
    for persist in (False,True):
        with pytest.raises(ReportPublicationRefused,match='without its activation'):
            runtime.bind_activation(p.a,p.configuration,persist=persist)
        assert not activation.exists()


def test_no_bonus_retry_refuses_changed_vector_and_request_symlink(prepared):
    p=prepared
    runtime.prepare_daily(p.a,p.vector)
    p.a._calculation_reference=p.raw
    with pytest.raises(ReportPublicationRefused,match='source or vector'):
        runtime.prepare_daily(p.a,{3:65534})
    (p.root/'report-state'/f'day-{p.day}.request.json').symlink_to(p.root/'missing')
    with pytest.raises(ReportPublicationRefused):
        runtime.prepare_daily(p.a,p.vector)


@pytest.mark.parametrize('last_outcome', ['verified', 'confirmed_failure'])
@pytest.mark.parametrize('resolution_format', [1, 2])
@pytest.mark.parametrize('boundary_version', [1, 2])
def test_weekly_r1_replays_signed_selection_and_passes_real_final_send(prepared, monkeypatch, last_outcome, resolution_format, boundary_version):
    """Real R0/R1/selection replay; only provider IPC result is synthetic here."""
    import copy
    from noah.core import report_publication
    from noah.reports.assignments import sign_week, earns_reporting_bonus
    from noah.reports.schedule import BOUNDARY, LEGACY_BOUNDARY, _resolve
    from noah.reports.tests.test_schedule import Chain, ANCHOR
    from noah.points.publication import check_before_send
    p=prepared
    bind_template(p.root,p.configuration,persist=True)
    signed=sign_week(unsigned_week(p),p.pair,
                     trusted_public_key=p.configuration['assignment_signer_public_key'])
    chain=Chain(shift=p.a.day_anchor_ts-ANCHOR,head=16000)
    monkeypatch.setattr(runtime,'resolve_schedule',lambda configuration,endpoints,day:
        _resolve(configuration,[chain,chain],day,
                 boundary_schema=LEGACY_BOUNDARY if boundary_version == 1 else BOUNDARY))
    policy,selection=runtime.select_weekly(p.configuration,signed,[],p.day,p.a.day_anchor_ts)
    if resolution_format==1:
        from noah.core.weekly_runtime import expand_resolution
        selection['resolution']=dict(schema='noah.weekly-resolution/1',week=signed,
            rounds=expand_resolution(p.configuration,signed,p.day,p.a.day_anchor_ts,selection['resolution']['base']))
    p.a.runner_chain.head_block=lambda:16000
    p.a.runner_chain.block_hash=chain.block_hash
    p.a.runner_chain.block_timestamp=lambda n:p.a.day_anchor_ts+n*12
    p.a.runner_chain.block_timestamp_ms=lambda n:(p.a.day_anchor_ts+n*12)*1000
    p.a.runner_chain.registration_blocks=lambda n:{3:1}
    request=runtime.request_for(p.a,p.raw,policy,signed['assignments'])
    digests=[runtime.semantic_digest(r) for r in policy['required_rounds']]
    per_round=[dict(round_sha256=d,effect='preserve',outcome='verified',
                    status='VERIFIED',reason='exact_match') for d in digests]
    if last_outcome!='verified':
        per_round[-1].update(effect='zero',outcome=last_outcome,status='MISSING',reason='reveal')
    seat=request['scored_seats'][0]
    decision={k:seat[k] for k in ('uid','hotkey','coldkey','registration_block')}
    decision.update(effect='preserve' if last_outcome=='verified' else 'zero',
                    outcome=last_outcome,rounds=per_round)
    result=dict(schema='noah.report-adjudication-result/1',manifest_sha256='cc'*32,
        request_sha256=runtime.semantic_digest(request),policy_sha256=request['policy_sha256'],
        status='COMPLETE',scored_decisions=[decision],rounds=[dict(round_sha256=d,
            required_roster_sha256='dd'*32,evaluation=request['evaluation'],
            readers=dict(reader_a='ee'*32,reader_b='ff'*32),engine_sha256='01'*32,
            calculation_sha256='02'*32,verdicts_sha256='03'*32,resolved=True) for d in digests])
    def replay(directory,package,frozen_request):
        assert frozen_request==request and package['sha256']==result['manifest_sha256']
        return copy.deepcopy(result)
    monkeypatch.setattr(report_publication,'replay_package',replay)
    package=dict(path='reports/'+result['manifest_sha256']+'/manifest.json',sha256=result['manifest_sha256'])
    hotkeys=artifact(p.root,verify(p.root,p.raw),'context')['scored_hotkeys']
    submission=dict(live_uids=[3],owner_uids=[0],fixture_hotkeys=hotkeys,chain_hotkeys=hotkeys)
    ref,vector=report_publication.retain_report(p.root,p.raw,request,package,submission,
                                                selection=selection,reward='bonus-20')
    saved = {path: path.read_bytes() for path in p.root.rglob('*.json')}
    monkeypatch.setattr(runtime, 'resolve_schedule', lambda *a, **k: pytest.fail('frozen weekly R1 must replay offline'))
    proof=report_publication.verify_report(p.root,ref)
    assert proof['selection'] == selection
    assert proof['request']['assignments']==signed['assignments']
    assert earns_reporting_bonus(decision,request)==(last_outcome=='verified')
    # A failed third report keeps ordinary V2 pay, instead of gate's zero/all-filtered refusal.
    assert vector==p.vector
    check_before_send(p.a,ref,vector)
    runtime.check_before_send(p.a,ref,vector)
    assert all(path.read_bytes() == raw for path, raw in saved.items())
    assert verify(p.root,ref)['artifacts']['agreed_score']==verify(p.root,p.raw)['artifacts']['agreed_score']
    # Neither a copied older week nor an independently self-signed authority can authorize final pay.
    p.configuration['assignment_signer_public_key']='ab'*32
    Path(p.a.report_gate_policy_path).write_bytes(encoded(p.configuration))
    with pytest.raises((ReportPublicationRefused,ValueError)):
        check_before_send(p.a,ref,vector)


def test_prospective_weekly_selection_replays_historical_gate_graph_unchanged(tmp_path, monkeypatch):
    from noah.core.tests.test_report_toggle_recovery import _ready
    from noah.core import report_reward_transition as transition
    from noah.points.publication import check_before_send
    a,client,raw,ref,vector,ordinary,calls,path=_ready(tmp_path,monkeypatch,
        'points-204040-v5',False,reward='gate')
    check_before_send(a,ref,vector)
    root=Path(a._submitted_record_dir)
    original={f:f.read_bytes() for f in root.rglob('*.json')}
    activation=json.loads((root/transition.ACTIVATION).read_bytes())
    old_configuration=json.loads(path.read_bytes())
    day=verify(root,raw)['day']
    sidecar=dict(schema=transition.SCHEMA,activation_sha256=transition.digest(activation),
        genesis_hash=activation['genesis_hash'],mining_netuid=a.netuid,day_anchor_ts=a.day_anchor_ts,
        effective_day=day+1,from_reward='gate',to_reward='bonus-20',
        announcement=dict(configuration=old_configuration,sha256=transition.digest(old_configuration)),
        observed=dict(block_number=201,block_hash=f'{201:064x}',
                      timestamp_ms=(a.day_anchor_ts+day*86400)*1000))
    transition.validate_transition(sidecar,activation)
    (root/transition.PATH).write_bytes(encoded(sidecar))
    config=schedule();config.pop('reported_netuid')
    config.update(schema='noah.weekly-assignment-template/2',genesis_hash=activation['genesis_hash'],
        mining_netuid=a.netuid,effective_day=activation['effective_day'],day_anchor_ts=a.day_anchor_ts,
        ark_coldkey=public_key_of(a.ark_coldkey).hex(),
        ark_validator_hotkey=public_key_of(a.designated_validator_hotkey).hex(),
        assignment_signer_public_key=sr25519.pair_from_seed(bytes([42])*32)[0].hex(),
        commitment_format='context-v1')
    path.write_bytes(encoded(config))
    # Model the required process restart, not a live mutation of its pinned transition.
    for name in ('_report_transition_digest','_report_reward_transition','_report_activation'):
        delattr(a,name)
    a.report_reward='bonus-20';a.weekly_assignments=True
    check_before_send(a,ref,vector)
    assert all(f.read_bytes()==data for f,data in original.items())
    assert not (root/AUTHORITY).exists()  # Old day needs only its original authenticated policy.
    assert runtime.selected_reward(a,day)=='gate'
    assert runtime.selected_reward(a,day+1)=='bonus-20'
