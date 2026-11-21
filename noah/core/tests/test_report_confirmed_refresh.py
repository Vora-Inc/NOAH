"""Upgrade refresh uses actual retained daily intent + maintained chain confirmation."""
import copy
import json
from pathlib import Path

import pytest

from noah.core import report_source_handoff as h, report_runtime as runtime, report_publication as publication
from noah.core.calculation_record import artifact, verify, encoded, digest, CalculationEvidenceRefused
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.reader import ChainUnreadable

REFUSALS = (publication.ReportPublicationRefused, PublicationStatusUnknown,
            ChainUnreadable, CalculationEvidenceRefused, OSError)
from noah.core.tests.test_weekly_runtime import prepared
from noah.core.tests.test_report_source_handoff import handoff, select, candidate
from noah.core.tests.test_weekly_report_retry_resume import result_for
from noah.core.tests.test_direct_runtime475 import prepared_boundary


@pytest.fixture
def published(handoff, monkeypatch, request):
    x=handoff;select(x,monkeypatch);p=x.p
    p.a._calculation_reference=x.successor
    hotkeys=artifact(p.root,verify(p.root,x.successor),'context')['scored_hotkeys']
    p.a.fixture_hotkeys=lambda _: {int(k):v for k,v in hotkeys.items()}
    p.a.live_uids=lambda _: sorted(p.a.runner_chain.seat_hotkeys(0))
    result=[]
    def worker(action,*args):
        if action=='adjudicate':
            destination=Path(args[3]);destination.mkdir();raw=encoded({'stub':'report transport'})
            (destination/'manifest.json').write_bytes(raw)
            result.append(result_for(x.request,digest(raw)))
        return copy.deepcopy(result[0])
    monkeypatch.setattr(runtime,'run_worker',worker);monkeypatch.setattr(publication,'run_worker',worker)
    x.vector=runtime.prepare_daily(p.a,p.vector)
    x.reference=p.a._calculation_reference
    x.proof=publication.verify_report(p.root,x.reference)
    x.height=verify(p.root,x.reference)['height']
    if getattr(request,'param',False):
        from noah.core.burn_allocation import allocate
        from noah.core.burn_publication import wrap_allocation
        pre=dict(x.vector);x.vector=allocate(pre,9800,0)
        p.a.runner_chain.subnet_owner=lambda block:('owner-cold','owner-hot')
        seats=p.a.runner_chain.seat_hotkeys(0)
        p.a.runner_chain.seat_hotkeys=lambda height:{**seats,0:'owner-hot'}
        owners=p.a.runner_chain.hotkey_owners
        p.a.runner_chain.hotkey_owners=lambda keys,height:{**owners(keys,height),'owner-hot':'owner-cold'}
        p.a.reader.recycle_or_burn=lambda netuid:'Burn'
        p.a.reader.max_weights_limit=lambda netuid:65535
        p.a.reader.min_allowed_weights=lambda netuid:1
        p.a.reader.epoch_consensus=lambda netuid,block=None:'Yuma'
        p.a.burn_allocation_bps=9800
        x.reference=wrap_allocation(p.root,x.reference,x.vector,{
            'bps':9800,'destination':{'uid':0,'hotkey':'owner-hot','coldkey':'owner-cold'},
            'pre_burn_u16':{str(k):v for k,v in pre.items()},
            'limits':{'max_weight_limit':65535,'min_allowed_weights':1,
                      'epoch_consensus':'Yuma','recycle_or_burn':'Burn'}})
        p.a._calculation_reference=x.reference
    # handoff() removes the synthetic initial daily record to model an unsent
    # day. Reset its fake chain history too before the genuine R1 first send.
    p.client.blocks.clear();p.client.last=0;p.client.events=[];p.client.extrinsics=[]
    p.client.block=p.client.finalized_head=110
    p.a.publish_direct(x.vector,x.height)
    x.path_daily=p.root/f'day-{p.day}.submitted.json'
    x.daily=x.path_daily.read_bytes()
    p.client.block=p.client.finalized_head=201
    # The pre-existing synthetic ordinary fixture omits dials; retain its exact
    # context and use the matching synthetic configuration for these tests.
    p.a.dials=None
    x.refresh={'epoch':200,'day':p.day,'height':x.height}
    monkeypatch.setattr(h,'source_identity',lambda:candidate('c'))
    return x


def check(x):
    h.check_send(x.p.a,x.proof,refresh=x.refresh,reference=x.reference,vector=x.vector)


def test_new_executor_refresh_reconfirms_real_daily_transaction(published):
    x=published;before={p:p.read_bytes() for p in x.p.root.rglob('*.json')}
    check(x)
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert x.path_daily.read_bytes()==x.daily


def test_real_epoch_path_refreshes_without_rewriting_daily_or_authority(published):
    from noah.core.epoch_work import republish
    x=published;calls=len(x.p.client.calls)
    assert republish(x.p.a,200,x.p.a.runner_chain.head_block())=='republished'
    assert len(x.p.client.calls)==calls+1
    assert x.path_daily.read_bytes()==x.daily and x.path.read_bytes()==x.raw


@pytest.mark.parametrize('field,value',[('epoch',199),('epoch',True),('day',99),('height',987)])
def test_refresh_context_must_match_current_epoch_and_receipt(published,field,value):
    x=published;x.refresh[field]=value
    with pytest.raises(REFUSALS):check(x)


@pytest.mark.parametrize('damage',['missing','unconfirmed','refresh','day','height','vector','reference','hash','transaction','identity','config','auth','pending'])
def test_forged_or_changed_daily_proof_refuses(published,monkeypatch,damage):
    x=published;a=x.p.a;record=json.loads(x.daily)
    if damage=='missing':x.path_daily.unlink()
    elif damage=='transaction':x.p.client.blocks[record['block']]['extrinsics']=[]
    elif damage=='identity':a.uid+=1
    elif damage=='config':a.dials={'changed':1}
    elif damage=='auth':monkeypatch.setattr(h,'configured',lambda _:({**x.value,'approval':'changed'},b''))
    elif damage=='pending':monkeypatch.setattr(a._publish_intent,'load',lambda:{'uncertain':True})
    else:
        m=record['direct_intent']
        if damage=='unconfirmed':m.pop('result')
        elif damage=='refresh':m['refresh_epoch']=100
        elif damage in ('day','height'):m[damage]+=1
        elif damage=='vector':m['vector']['3']-=1
        elif damage=='reference':m['calculation_evidence']['sha256']='f'*64
        elif damage=='hash':m['result']['block_hash']='0x'+'f'*64;record['block_hash']=m['result']['block_hash']
        x.path_daily.write_bytes(encoded(record))
    with pytest.raises(REFUSALS):check(x)


def test_changed_executor_cannot_first_send_or_new_burn_zero(published):
    x=published
    with pytest.raises(publication.ReportPublicationRefused):h.check_send(x.p.a,x.proof)
    # The adapter's replacement branch deliberately does not forward refresh
    # context, even when a caller supplies an epoch alongside a successor.
    with pytest.raises(publication.ReportPublicationRefused):
        x.p.a.publish_direct(x.vector,x.height,source_day=x.p.day,refresh_epoch=200,successor={'kind':'burn-zero'})


@pytest.mark.parametrize('damage',['receipt','request','authority','epoch','pending'])
def test_mutation_during_paired_reconciliation_refuses(published,monkeypatch,damage):
    from noah.core import direct
    x=published;original=direct.reconcile
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        if damage=='receipt':x.path_daily.write_bytes(x.daily+b' ')
        elif damage=='request':x.path.write_bytes(x.raw+b' ')
        elif damage=='authority':monkeypatch.setattr(h,'configured',lambda _:({**x.value,'approval':'changed'},b''))
        elif damage=='epoch':x.p.client.epoch+=1
        else:monkeypatch.setattr(x.p.a._publish_intent,'load',lambda:{'uncertain':True})
        return result
    monkeypatch.setattr(direct,'reconcile',changed)
    with pytest.raises(REFUSALS):check(x)


def test_same_source_retains_existing_behavior(published,monkeypatch):
    x=published;monkeypatch.setattr(h,'source_identity',lambda:candidate('b'))
    x.path_daily.unlink()
    h.check_send(x.p.a,x.proof)


def test_real_outage_refresh_keeps_original_daily_deadline(published,monkeypatch):
    from noah.points.outage import republish_plain
    x=published;a=x.p.a
    monkeypatch.setattr(a.runner_chain,'head_block',lambda:22000)
    (x.p.root/'points-outage.json').write_bytes(encoded({
        'schema':'noah-points-outage/1','policy':a.points_policy.manifest,'reason':'ChainUnreadable'}))
    before=x.path_daily.read_bytes()
    assert republish_plain(a,200,22000,before_send=lambda:None)=='points_outage_refreshed'
    assert x.path_daily.read_bytes()==before
    assert a._report_refresh_attempt is None


@pytest.mark.parametrize('damage',['prepared','result','vector'])
def test_final_guard_refuses_changed_owned_attempt_and_clears_authority(published,monkeypatch,damage):
    from noah.core import chain_adapter
    from noah.core.publish import PublicationRefused
    x=published;a=x.p.a;original=chain_adapter.publish_weights_direct;calls=len(x.p.client.calls)
    def alter(*args,**kwargs):
        if damage=='prepared':a._publish_intent.annotate(prepared_transaction={'unexpected':True})
        elif damage=='result':a._publish_intent.annotate(result={'block':111,'extrinsic_index':2})
        else:a._publish_intent.annotate(vector={'3':1})
        return original(*args,**kwargs)
    monkeypatch.setattr(chain_adapter,'publish_weights_direct',alter)
    with pytest.raises(PublicationRefused):
        a.publish_direct(x.vector,x.height,refresh_epoch=200,source_day=x.p.day)
    assert len(x.p.client.calls)==calls
    assert a._report_refresh_attempt is None


def test_uncertain_refresh_loses_attempt_authority_and_reconciles_without_resend(published):
    from noah.core.publish import NotOnChain
    from noah.core.epoch_work import republish
    x=published;a=x.p.a;x.p.client.lost_reply=True;calls=len(x.p.client.calls)
    with pytest.raises(NotOnChain):
        a.publish_direct(x.vector,x.height,refresh_epoch=200,source_day=x.p.day)
    assert a._report_refresh_attempt is None and a._publish_intent.load() is not None
    with pytest.raises(publication.ReportPublicationRefused):check(x)
    x.p.client.lost_reply=False
    assert republish(a,200,a.runner_chain.head_block())=='already_published_this_epoch'
    assert len(x.p.client.calls)==calls+1 and a._publish_intent.load() is None
    assert x.path_daily.read_bytes()==x.daily


def test_pending_marker_mutation_inside_final_reconcile_never_sends(published,monkeypatch):
    from noah.core import direct
    from noah.core.publish import PublicationRefused
    x=published;a=x.p.a;original=direct.reconcile;calls=len(x.p.client.calls)
    def mutate(*args,**kwargs):
        result=original(*args,**kwargs)
        if a._publish_intent.load() is not None:a._publish_intent.annotate(height=x.height+1)
        return result
    monkeypatch.setattr(direct,'reconcile',mutate)
    with pytest.raises(PublicationRefused):
        a.publish_direct(x.vector,x.height,refresh_epoch=200,source_day=x.p.day)
    assert len(x.p.client.calls)==calls and a._report_refresh_attempt is None


@pytest.mark.parametrize('published',[True],indirect=True)
def test_real_burn_wrapped_refresh_preserves_outer_vector_and_original_r1(published):
    from noah.core.epoch_work import republish
    x=published;check(x)
    assert x.vector[0]>0
    assert republish(x.p.a,200,x.p.a.runner_chain.head_block())=='republished'
    assert x.path_daily.read_bytes()==x.daily
    # An inner R1 is not the already-confirmed outer burn allocation.
    x.reference=x.p.a._calculation_reference=x.proof['source']
    with pytest.raises(REFUSALS):check(x)


@pytest.mark.parametrize('published',[True],indirect=True)
def test_already_confirmed_zero_burn_replacement_refreshes_under_new_executor(published,monkeypatch):
    from noah.core.epoch_work import republish
    from noah.core.burn_publication import confirmed_transition
    x=published;a=x.p.a
    # The new burn-zero publication still requires its original authorized
    # executor. Only its subsequently confirmed unchanged receipt may refresh.
    monkeypatch.setattr(h,'source_identity',lambda:candidate('b'))
    a.burn_allocation_bps=0
    assert republish(a,200,a.runner_chain.head_block())=='burn_cleared'
    replacement=confirmed_transition(x.p.root,x.p.day)
    assert replacement is not None and replacement['direct_intent']['successor_kind']=='burn-zero'
    monkeypatch.setattr(h,'source_identity',lambda:candidate('c'))
    x.p.client.epoch=300;x.p.client.block=x.p.client.finalized_head=301
    assert republish(a,300,a.runner_chain.head_block())=='republished'
    assert confirmed_transition(x.p.root,x.p.day)==replacement
    assert x.path_daily.read_bytes()==x.daily


@pytest.mark.parametrize('field,value',[('epoch',True),('day',True),('height',True)])
def test_boolean_refresh_fields_are_not_integer_authority(published,field,value):
    x=published;x.refresh[field]=value
    with pytest.raises(REFUSALS):check(x)


def test_unknown_reference_fields_and_boolean_weights_refuse(published):
    x=published;x.reference={**x.reference,'unknown':'field'}
    with pytest.raises(REFUSALS):check(x)
    x.reference=x.p.a._calculation_reference;x.vector={3:True}
    with pytest.raises(REFUSALS):check(x)


def test_nonobject_canonical_receipt_is_a_named_refusal(published):
    x=published;x.path_daily.write_bytes(encoded([]))
    with pytest.raises(publication.ReportPublicationRefused,match='not an object'):check(x)
