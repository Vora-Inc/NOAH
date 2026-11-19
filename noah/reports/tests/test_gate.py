"""Independent gate toggle against the actual V2.0 Python/TypeScript engines."""
import copy
import json
from pathlib import Path

import pytest

from noah.points import onboarding
from noah.points.run import agreed
from noah.points.tests.test_no_pool import economics_fixture
from noah.reports.common import digest
from noah.reports.gate import apply_backing_gate
from noah.reports.validator import score_preview


def setup():
    raw=economics_fixture([[(0,0,0),(0,0,0)]]*3)
    raw.update(pointsRules=onboarding.RULES,pointsExcludedUids=[2])
    from noah.reports.tests.sample import fixture
    r=fixture()['round']
    anchor=r['block_timestamp_ms']//1000-86400
    raw['dayEpochTs']=str(anchor)
    bundle=dict(rounds=[r],day_anchor_ts=anchor,evaluated_at_ms=r['reveal_close_ms'],
                genesis_hash=r['genesis_hash'],netuid=82,seats={'0':'aa'*32,'1':'bb'*32,'2':'cc'*32},
                participation={'0':0,'1':0,'2':0},registration_blocks={'100':{'0':1,'1':1,'2':1}})
    records={}
    for uid,hotkey in bundle['seats'].items():
        records[hotkey]=dict(round_sha256=digest(r),uid=int(uid),hotkey=hotkey,
                            genesis_hash=r['genesis_hash'],netuid=82,eval_day=1,status='VERIFIED',reason='exact_match')
    bundle['verdicts']={digest(r):records}
    return raw,bundle,records


def test_off_is_byte_identical_and_does_not_read_reports():
    raw,bundle,_=setup(); original=agreed(raw)
    before=json.dumps(original,sort_keys=True).encode()
    assert apply_backing_gate(original,None,enabled=False) is original
    preview=score_preview(raw,None,None,None)
    assert json.dumps(preview['effective'],sort_keys=True).encode()==before
    assert preview['original']['weightsU16']==original['weightsU16']
    assert not preview['publishable'] and not preview['gate_enabled']


@pytest.mark.parametrize('status,reason',[('MISSING','commit'),('MISSING','reveal'),('MISMATCH','outputs_differ')])
def test_confirmed_failure_changes_only_effective_payout(status,reason):
    raw,bundle,records=setup();original=agreed(raw);saved=copy.deepcopy(original)
    records['aa'*32].update(status=status,reason=reason)
    gated=apply_backing_gate(original,bundle,enabled=True)
    assert original==saved
    assert gated['weightsU16']=={'0':0,'1':65535,'2':0}
    for before,after in zip(original['miners'],gated['miners']):
        assert {key:after[key] for key in before}==before
    assert gated['miners'][0]['effectivePayWeightFp']=='0'
    assert gated['miners'][0]['backingGate']['outcome']=='confirmed_failure'


def test_unresolved_and_open_round_never_zero_or_reintroduce_excluded_uid():
    raw,bundle,records=setup();original=agreed(raw)
    records['aa'*32].update(status='UNVERIFIABLE',reason='eligibility_history_incomplete')
    result=apply_backing_gate(original,bundle,enabled=True)
    assert result['weightsU16']==original['weightsU16']
    assert result['miners'][0]['backingGate']['outcome']=='unresolved'
    assert result['weightsU16']['2']==0
    records['aa'*32].update(status='MISSING',reason='commit')
    bundle['evaluated_at_ms']-=1
    assert apply_backing_gate(original,bundle,enabled=True)['weightsU16']==original['weightsU16']


def test_all_failed_declines_new_vector_and_leaves_raw_points():
    raw,bundle,records=setup();original=agreed(raw)
    for r in records.values(): r.update(status='MISSING',reason='commit')
    result=apply_backing_gate(original,bundle,enabled=True)
    assert result['weightsU16']=={}
    assert result['backingGate']['declinedNewVector']
    assert result['miners'][0]['scoreFp']==original['miners'][0]['scoreFp']


def test_absent_raw_package_is_unresolved_even_with_forged_saved_verdict(tmp_path):
    raw,bundle,_=setup();r=bundle['rounds'][0]
    package=tmp_path/'forged';package.mkdir()
    (package/'round.json').write_text(json.dumps(r))
    (package/'evidence.json').write_text(json.dumps({'evidence':{'aa'*32:{'status':'MISSING'}}}))
    preview=score_preview(raw,bundle,[r],tmp_path,enabled=True)
    assert preview['effective']['weightsU16']==preview['original']['weightsU16']
    assert preview['effective']['miners'][0]['backingGate']['outcome']=='unresolved'
    assert not preview['publishable']
