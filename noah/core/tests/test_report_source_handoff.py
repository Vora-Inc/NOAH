"""Exact-request producer handoff; original history is never relabeled."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from noah.core import report_source_handoff as h, report_runtime as runtime
from noah.core.calculation_record import CalculationEvidenceRefused, artifact, digest, encoded, retain, verify
from noah.core.report_publication import ReportPublicationRefused, semantic_digest
from noah.core.tests.test_weekly_runtime import prepared
from noah.core.tests.test_weekly_report_retry_resume import signed_week_setup


def candidate(char):
    files = {'noah/reports/budget.py': char * 64, 'noah/core/calculation_record.py': char * 64}
    return {'declared_commit':char * 40, 'files':files, 'source_sha256':digest(encoded(files))}


def make_raw(p, source, height):
    m = verify(p.root, p.raw)
    c = artifact(p.root, m, 'context');c['candidate'] = source;c['height'] = height
    return retain(p.root, fixture=artifact(p.root,m,'fixture'),
                  agreed_text=encoded(artifact(p.root,m,'agreed_score')).decode(),
                  vector=p.vector,context=c,canonical_day=False)


@pytest.fixture
def handoff(prepared, monkeypatch):
    p = prepared
    # This shared fixture creates a synthetic published day. Model an unsent
    # outage here; production handoff never removes these guards.
    for kind in ("submitted", "confirmed", "accounted"):
        (p.root / f"day-{p.day}.{kind}.json").unlink(missing_ok=True)
    p.a._commit_ledger = None
    height = verify(p.root,p.raw)['height']
    p.raw = make_raw(p,candidate('a'),height)
    p.a._calculation_reference = p.raw
    path = signed_week_setup(p,monkeypatch)
    def unavailable(*args):raise ReportPublicationRefused('synthetic provider outage')
    monkeypatch.setattr(runtime,'run_worker',unavailable)
    with pytest.raises(ReportPublicationRefused,match='synthetic provider outage'):
        runtime.prepare_daily(p.a,p.vector)
    request_raw = path.read_bytes();request=json.loads(request_raw)
    successor=make_raw(p,candidate('b'),height+1)
    original,_,_,financial=h.raw_pair(p.root,successor,p.raw)
    def release(char):return {'release_id':char*12+'-'+char*16,'commit':char*40,'archive_sha256':char*64,
                              'manifest_sha256':char*64,'candidate':candidate(char)}
    value=dict(schema=h.SCHEMA,day=p.day,genesis_hash=original['genesis_hash'],netuid=original['netuid'],
               request_sha256=digest(request_raw),request_semantic_sha256=semantic_digest(request),
               source=p.raw,successor=successor,from_release=release('a'),to_release=release('b'),
               financial_sha256=financial,replay={k:original['artifacts']['agreed_score']['sha256']
               for k in ('old_python','old_typescript','new_python','new_typescript')},
               approval='synthetic operator approval',checkpoint_sha256='c'*64)
    monkeypatch.setattr(h,'source_identity',lambda:candidate('b'))
    return SimpleNamespace(p=p,value=value,request=request,raw=request_raw,path=path,successor=successor)


def select(x,monkeypatch):
    monkeypatch.setattr(h,'configured',lambda adapter:(x.value,encoded(x.value)))


def test_default_absent_still_refuses_source_change(handoff):
    x=handoff
    with pytest.raises(ReportPublicationRefused,match='lacks operator handoff'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)
    assert x.path.read_bytes()==x.raw


def test_authorized_later_attempt_preserves_raw_request_and_provenance(handoff,monkeypatch):
    x=handoff;select(x,monkeypatch)
    original={p:p.read_bytes() for p in x.p.root.rglob('*.json')}
    ref,m=runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)
    assert ref==x.p.raw and m==verify(x.p.root,x.p.raw)
    proof={'source':x.p.raw,'request':x.request,'source_handoff':x.p.a._report_source_handoff_reference}
    assert h.verify_proof(x.p.root,proof)==x.value
    h.check_send(x.p.a,proof)
    later=make_raw(x.p,candidate('b'),verify(x.p.root,x.successor)['height']+1)
    assert runtime.resume_raw_source(x.p.a,later,x.p.raw,request_raw=x.raw)[0]==x.p.raw
    assert all(p.read_bytes()==raw for p,raw in original.items())
    assert x.path.read_bytes()==x.raw


@pytest.mark.parametrize('field',['from_block','to_block','day_start_utc_unix','profile_sha256',
                                 'scoring_mode','points_policy','scored_hotkeys','foreign_valuations'])
def test_authorization_never_ignores_other_context_drift(handoff,monkeypatch,field):
    x=handoff;select(x,monkeypatch)
    m=verify(x.p.root,x.successor);context=artifact(x.p.root,m,'context');context[field]='tampered'
    altered=retain(x.p.root,fixture=artifact(x.p.root,m,'fixture'),
                   agreed_text=encoded(artifact(x.p.root,m,'agreed_score')).decode(),
                   vector=x.p.vector,context=context,canonical_day=False)
    with pytest.raises(ReportPublicationRefused,match='financial context'):
        runtime.resume_raw_source(x.p.a,altered,x.p.raw,request_raw=x.raw)


@pytest.mark.parametrize('field',['source','successor','request_sha256','request_semantic_sha256','financial_sha256',
                                 'from_release','to_release','replay','day','genesis_hash','netuid'])
def test_changed_document_bindings_refuse(handoff,field):
    x=handoff;v=copy.deepcopy(x.value)
    if field in ('source','successor'):v[field]['sha256']='d'*64
    elif field in ('from_release','to_release'):v[field]['candidate']=candidate('d')
    elif field=='replay':v[field]['new_python']='d'*64
    elif field in ('day','netuid'):v[field]+=1
    elif field=='genesis_hash':v[field]='0x'+'d'*64
    else:v[field]='d'*64
    with pytest.raises((ReportPublicationRefused,CalculationEvidenceRefused,OSError,KeyError)):
        h.verify_graph(x.p.root,v,x.raw)


def test_source_handoff_cannot_authorize_no_bonus_branch(handoff,monkeypatch):
    x=handoff;select(x,monkeypatch)
    with pytest.raises(ReportPublicationRefused,match='context differs'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw)


def test_running_source_and_pending_intent_refuse(handoff,monkeypatch):
    x=handoff;select(x,monkeypatch)
    monkeypatch.setattr(h,'source_identity',lambda:candidate('c'))
    with pytest.raises(ReportPublicationRefused,match='running producer'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)
    x.p.a._publish_intent=SimpleNamespace(load=lambda:{'pending':True})
    with pytest.raises(ReportPublicationRefused,match='unresolved publication intent'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)


def test_proof_missing_changed_or_wrong_request_refuses(handoff,monkeypatch):
    x=handoff;select(x,monkeypatch)
    runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)
    proof={'source':x.p.raw,'request':x.request,'source_handoff':x.p.a._report_source_handoff_reference}
    assert h.verify_proof(x.p.root,{'source':x.p.raw,'request':x.request}) is None
    x.path.write_bytes(x.raw+b' ')
    with pytest.raises(ReportPublicationRefused,match='frozen request differs'):
        h.verify_proof(x.p.root,proof)
    x.path.write_bytes(x.raw)
    (x.p.root/proof['source_handoff']['path']).unlink()
    with pytest.raises(OSError):h.verify_proof(x.p.root,proof)


def test_document_strictness_and_file_shapes(handoff,tmp_path):
    x=handoff
    assert h.document(encoded(x.value))==x.value
    with pytest.raises(ReportPublicationRefused):h.document(json.dumps(x.value).encode())
    path=tmp_path/'authorization.json';path.write_bytes(encoded(x.value))
    assert h.read_bytes(path)==encoded(x.value)
    linked=tmp_path/'linked';linked.symlink_to(path)
    with pytest.raises(OSError):h.read_bytes(linked)
    import os
    alias=tmp_path/'alias';os.link(path,alias)
    with pytest.raises(ReportPublicationRefused,match='file shape'):h.read_bytes(path)
    with pytest.raises(ReportPublicationRefused,match='ancestor|operator controlled'):
        h.read_bytes(path,protected=True)


@pytest.mark.parametrize('kind',['submitted','confirmed','accounted'])
def test_existing_disposition_cannot_be_reopened(handoff,monkeypatch,kind):
    x=handoff;select(x,monkeypatch)
    (x.p.root/f'day-{x.p.day}.{kind}.json').write_text('{}')
    with pytest.raises(ReportPublicationRefused,match='already has a disposition'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)


def test_commit_ledger_entry_cannot_be_reopened(handoff,monkeypatch):
    x=handoff;select(x,monkeypatch)
    x.p.a._commit_ledger=SimpleNamespace(load=lambda:{x.p.day:{}})
    with pytest.raises(ReportPublicationRefused,match='commit ledger'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)


@pytest.mark.parametrize('extra',[
 {'NOAH_REPORT_SOURCE_HANDOFF':'/etc/noah/handoff.json'},
 {'NOAH_REPORT_SOURCE_HANDOFF_SHA256':'a'*64},
 {'NOAH_REPORT_SOURCE_HANDOFF':'relative','NOAH_REPORT_SOURCE_HANDOFF_SHA256':'a'*64},
 {'NOAH_REPORT_SOURCE_HANDOFF':'/etc/noah/handoff.json','NOAH_REPORT_SOURCE_HANDOFF_SHA256':'bad'},
])
def test_settings_refuse_incomplete_or_invalid_authority(extra):
    from noah.core.tests.test_settings import COMPLETE
    from noah.core.settings import Settings,BadSetting
    base={**COMPLETE,'NOAH_REPORT_GATE_ENABLED':'true','NOAH_REPORT_GATE_POLICY':'/etc/noah/policy.json'}
    with pytest.raises(BadSetting):Settings.from_mapping({**base,**extra})


def test_settings_default_absent_and_paired_selection():
    from noah.core.tests.test_settings import COMPLETE
    from noah.core.settings import Settings,BadSetting
    assert Settings.from_mapping(COMPLETE).report_source_handoff is None
    selection={'NOAH_REPORT_SOURCE_HANDOFF':'/etc/noah/handoff.json','NOAH_REPORT_SOURCE_HANDOFF_SHA256':'a'*64}
    with pytest.raises(BadSetting):Settings.from_mapping({**COMPLETE,**selection})
    settings=Settings.from_mapping({**COMPLETE,**selection,'NOAH_REPORT_GATE_ENABLED':'true',
                                    'NOAH_REPORT_GATE_POLICY':'/etc/noah/policy.json'})
    assert settings.report_source_handoff==selection['NOAH_REPORT_SOURCE_HANDOFF']


def test_full_report_r1_replays_old_economics_with_explicit_executor_provenance(handoff,monkeypatch):
    from noah.core import report_publication as publication
    from noah.core.tests.test_weekly_report_retry_resume import result_for
    x=handoff;select(x,monkeypatch);p=x.p
    p.a._calculation_reference=x.successor
    hotkeys=artifact(p.root,verify(p.root,x.successor),'context')['scored_hotkeys']
    p.a.fixture_hotkeys=lambda _: {int(k):v for k,v in hotkeys.items()}
    p.a.live_uids=lambda _:sorted(p.a.runner_chain.seat_hotkeys(0))
    calls=[];result=[]
    def worker(action,*args):
        if action=='adjudicate':
            invocation=json.loads(Path(args[1]).read_bytes());calls.append(invocation['request'])
            destination=Path(args[3]);destination.mkdir();raw=encoded({'stub':'report transport'})
            (destination/'manifest.json').write_bytes(raw)
            result.append(result_for(x.request,digest(raw)))
        return copy.deepcopy(result[0])
    monkeypatch.setattr(runtime,'run_worker',worker);monkeypatch.setattr(publication,'run_worker',worker)
    actual=runtime.prepare_daily(p.a,p.vector)
    proof=publication.verify_report(p.root,p.a._calculation_reference)
    assert proof['source']==p.raw and proof['request']==x.request
    assert h.verify_proof(p.root,proof)==x.value and calls==[x.request]
    assert artifact(p.root,verify(p.root,p.a._calculation_reference),'context')['candidate']==candidate('a')
    assert x.path.read_bytes()==x.raw
    runtime.check_before_send(p.a,p.a._calculation_reference,actual)
    monkeypatch.setattr(h,'configured',lambda _:({**x.value,'approval':'different approval'},b''))
    with pytest.raises(ReportPublicationRefused,match='authorization changed'):
        runtime.check_before_send(p.a,p.a._calculation_reference,actual)
    # Historical proof remains independently verifiable after live config changes.
    assert publication.verify_report(p.root,p.a._calculation_reference)==proof


@pytest.mark.parametrize('damage',[None,'owner','mode','parent','digest','checkpoint'])
def test_protected_authority_custody_and_exact_pin(tmp_path,monkeypatch,handoff,damage):
    import os,stat
    x=handoff;path=tmp_path/'authority.json';path.write_bytes(encoded(x.value));path.chmod(0o640)
    if damage=='checkpoint':
        changed={**x.value,'checkpoint_sha256':'f'*64}
        path.write_bytes(encoded(changed))
    original_lstat=Path.lstat;original_fstat=os.fstat
    def info(value,*,parent=False):
        values={name:getattr(value,name) for name in dir(value) if name.startswith('st_')}
        values['st_uid']=1 if damage=='owner' and not parent else 0
        if parent:values['st_mode']=stat.S_IFDIR | (0o777 if damage=='parent' else 0o755)
        elif damage=='mode':values['st_mode']=stat.S_IFREG|0o664
        return SimpleNamespace(**values)
    monkeypatch.setattr(Path,'lstat',lambda p:info(original_lstat(p),parent=p!=path))
    monkeypatch.setattr(os,'fstat',lambda fd:info(original_fstat(fd)))
    adapter=SimpleNamespace(report_source_handoff=str(path),report_source_handoff_sha256=
                            'f'*64 if damage=='digest' else digest(encoded(x.value)))
    if damage:
        with pytest.raises(ReportPublicationRefused):h.configured(adapter)
    else:assert h.configured(adapter)==(x.value,encoded(x.value))


def test_uncertain_intent_refuses_before_handoff_authorization(handoff,monkeypatch):
    x=handoff
    def never_authorize(*args):raise AssertionError('handoff must not run with an unresolved intent')
    monkeypatch.setattr(h,'authorize',never_authorize)
    before={path:path.read_bytes() for path in x.p.root.rglob('*.json')}
    x.p.a._publish_intent=SimpleNamespace(load=lambda:{'uncertain':True})
    with pytest.raises(ReportPublicationRefused,match='unresolved publication intent'):
        runtime.resume_raw_source(x.p.a,x.successor,x.p.raw,request_raw=x.raw)
    assert all(path.read_bytes()==raw for path,raw in before.items())
