"""Long weekly deadlines select by validated shape, never by arbitrary override."""
import copy
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from noah.core import report_publication as publication
from noah.core.calculation_record import encoded,digest
from noah.reports.adjudication_contract import validate_request
from noah.reports.common import digest as semantic
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.tests.test_adjudication_contract import request as basic_request


def weekly():
    request=basic_request()
    first={**request['policy']['required_rounds'][0], 'commitment_format':'context-v1',
           'eligibility_policy_version':REPORT_POLICY}
    second={**first,'reported_netuid':65,'release_prefix':'89'*16}
    request['policy'].update(required_rounds=[first,second],eligibility_policy_version=REPORT_POLICY)
    request['policy_sha256']=semantic(request['policy'])
    seat=request['scored_seats'][0]
    request['assignments']=[{k:seat[k] for k in ('uid','hotkey','coldkey','registration_block')}|{'targets':[64,65]}]
    validate_request(request)
    return request


def inputs(tmp_path, *, shared=True,schema='noah.report-adjudication-manifest/3'):
    request=weekly() if shared else basic_request()
    invocation={'schema':'noah.report-adjudication-invocation/1','request':request,'sources':[
        {'id':name,'endpoint':url,'history':None} for name,url in
        [('reader_a','ws://a.invalid'),('reader_b','wss://b.invalid')]]}
    req=tmp_path/'request.json';req.write_bytes(encoded(request))
    invoke=tmp_path/'invocation.json';invoke.write_bytes(encoded(invocation))
    package=tmp_path/'package';package.mkdir()
    raw=encoded({'schema':schema,'request':request});(package/'manifest.json').write_bytes(raw)
    return request,invoke,['--request',req,'--package',package,'--manifest-sha256',digest(raw)]


def test_only_weekly_collection_and_package3_replay_extend_timeout(tmp_path):
    _,invoke,args=inputs(tmp_path)
    assert publication.worker_timeout('adjudicate',['--invocation',invoke])==3900
    assert publication.worker_timeout('adjudicate-verify',args)==3900
    assert publication.worker_timeout('assignment-week',[])==120
    assert publication.worker_timeout('resolve-day',[])==900


@pytest.mark.parametrize('schema',['noah.report-adjudication-manifest/1','noah.report-adjudication-manifest/2'])
def test_historical_replay_keeps_900(tmp_path,schema):
    _,_,args=inputs(tmp_path,schema=schema)
    assert publication.worker_timeout('adjudicate-verify',args)==900


def test_old_nonweekly_collection_keeps_900(tmp_path):
    _,invoke,_=inputs(tmp_path,shared=False)
    assert publication.worker_timeout('adjudicate',['--invocation',invoke])==900


@pytest.mark.parametrize('damage',['pin','mismatch','duplicate-flag','unknown-schema','symlink','hardlink','oversize','duplicate-json','fifo'])
def test_invalid_or_ambiguous_profile_refuses_before_worker(tmp_path,damage,monkeypatch):
    request,invoke,args=inputs(tmp_path)
    manifest=tmp_path/'package/manifest.json'
    if damage=='pin':args[-1]='0'*64
    elif damage=='mismatch':
        request['calculation']['fixture_sha256']='ab'*32
        (tmp_path/'request.json').write_bytes(encoded(request))
    elif damage=='duplicate-flag':args+=['--request',tmp_path/'request.json']
    elif damage=='unknown-schema':
        raw=encoded({'schema':'noah.report-adjudication-manifest/99','request':request});manifest.write_bytes(raw);args[-1]=digest(raw)
    elif damage=='symlink':
        original=manifest.with_name('original');manifest.rename(original);manifest.symlink_to(original)
    elif damage=='hardlink':os.link(manifest,manifest.with_name('alias'))
    elif damage=='fifo':manifest.unlink();os.mkfifo(manifest)
    elif damage=='oversize':manifest.write_bytes(b' '* (16*1024**2+1))
    else:
        raw=manifest.read_bytes().replace(b'{',b'{"schema":"duplicate",',1);manifest.write_bytes(raw);args[-1]=digest(raw)
    monkeypatch.setattr(publication.subprocess,'run',lambda *_a,**_k:pytest.fail('child must not run'))
    with pytest.raises(publication.ReportPublicationRefused):publication.run_worker('adjudicate-verify',*args)


def test_actual_parent_timeout_and_timeout_failure_preserve_request(tmp_path,monkeypatch):
    _,invoke,_=inputs(tmp_path);original=invoke.read_bytes();seen=[]
    monkeypatch.setattr(publication,'worker_python',lambda:'/selected/reports/python')
    def run(command,**options):
        seen.append(options['timeout'])
        assert command[:3]==['/selected/reports/python','-I','-m']
        options['stdout'].write(b'{}');return SimpleNamespace(returncode=0)
    monkeypatch.setattr(publication.subprocess,'run',run)
    assert publication.run_worker('adjudicate','--invocation',invoke)=={}
    assert seen==[3900] and invoke.read_bytes()==original
    def timeout(*_a,**_k):raise subprocess.TimeoutExpired('private-worker',3900)
    monkeypatch.setattr(publication.subprocess,'run',timeout)
    with pytest.raises(publication.ReportPublicationRefused,match='could not complete'):
        publication.run_worker('adjudicate','--invocation',invoke)
    assert invoke.read_bytes()==original
