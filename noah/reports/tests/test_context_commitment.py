"""Opt-in context commitments; all calls, wallets and chain responses are synthetic."""
import copy
from dataclasses import replace
import hashlib
import itertools
import json
from types import SimpleNamespace

import pytest

from noah.reports import miner
from noah.reports.backing.protocol.context_commitment import (
    DOMAIN, ENVELOPE, FORMAT, build_commitment, decode_commitment, matches_context,
    matches_reveal, round_id,
)
from noah.reports.backing.protocol.preimage import REPORT_POLICY, commitment_of
from noah.reports.backing.select import select_for_miner
from noah.reports.backing.prepare import load_prepared, PrepareError
from noah.reports.backing.submit import SubmitError
from noah.reports.backing.chain_session import commitment_info, context_commitment_of
from noah.reports.common import digest, state_directory
from noah.reports.tests.test_report_only import report_case
from noah.reports.tests.test_miner_commands import case, sign, no_sign
from noah.reports.verification import validate_round, verify_shadow


def context_case(netuid=64):
    r, e, payload = report_case()
    r.update(commitment_format=FORMAT, reported_netuid=netuid)
    payload = replace(payload, netuid=netuid)
    e['calculation']['netuid'] = netuid
    e['reveal']['data_hex'] = payload.encode().hex()
    e['commit']['data_hex'] = build_commitment(r, payload.encode()).hex()
    return r, e, payload


def choose(calls, r, hotkey):
    return select_for_miner(calls, hotkey_hex=hotkey, mining_netuid=r['mining_netuid'], expected_round=r,
        **{key:r[key] for key in ('commit_open_ms','commit_close_ms','reveal_open_ms','reveal_close_ms')})


def test_exact_envelope_layout_and_full_round_domain_binding():
    r,e,p = context_case()
    raw = bytes.fromhex(e['commit']['data_hex'])
    assert ENVELOPE.size == len(raw) == 79 < 256
    assert raw[:11] == DOMAIN and raw[11:15] == bytes.fromhex('52004000')
    assert raw[15:47] == bytes.fromhex(digest(r)) == round_id(r)
    assert raw[47:] == hashlib.sha256(raw[:47]+p.encode()).digest()
    assert decode_commitment(raw).reported_netuid == 64
    assert matches_context(raw,r) and matches_reveal(raw,p.encode())
    assert verify_shadow(r,e)['status'] == 'VERIFIED'
    assert round_id(dict(reversed(list(r.items())))) == round_id(r)
    assert round_id({**r,'reveal_close_ms':r['reveal_close_ms']+1}) != round_id(r)
    assert commitment_of(p.encode()) != raw[47:]


PERMUTATIONS = list(itertools.permutations(range(3)))
@pytest.mark.parametrize('commit_order',PERMUTATIONS)
@pytest.mark.parametrize('reveal_order',PERMUTATIONS)
@pytest.mark.parametrize('missing',[None,0,2])
def test_any_order_and_missing_first_or_third_preserves_other_reports(commit_order,reveal_order,missing):
    cases = [context_case(n) for n in (7,18,64)]
    calls=[]
    for leg, order in (('commit',commit_order),('reveal',reveal_order)):
        for position,index in enumerate(order):
            if leg=='reveal' and index==missing:
                continue
            row=copy.deepcopy(cases[index][1][leg]);row['structurally_valid']=True
            row['block_number']+=position;row['timestamp_ms']+=position
            calls.append(row)
    calls.reverse() # Input iteration order must not choose the winner.
    for index,(r,e,_) in enumerate(cases):
        selected=choose(calls,r,e['snapshot']['hotkey'])
        assert selected['commit'] is not None
        assert (selected['reveal'] is None)==(index==missing)
        e.update(commit=selected['commit'],reveal=selected['reveal'])
        result=verify_shadow(r,e)
        assert result['status']==('MISSING' if index==missing else 'VERIFIED')
        if index==missing:assert result['reason']=='reveal'


def test_unsent_third_has_no_other_targets_pair():
    cases=[context_case(n) for n in (7,18,64)]
    calls=[{**e[leg],'structurally_valid':True} for _,e,_ in cases[:2] for leg in ('commit','reveal')]
    r,e,_=cases[2];selected=choose(calls,r,e['snapshot']['hotkey'])
    assert selected['commit'] is selected['reveal'] is None
    e.update(commit=None,reveal=None)
    assert verify_shadow(r,e)['reason']=='commit'


@pytest.mark.parametrize('reveal_first',[False,True])
def test_first_successful_context_commit_stands_even_if_later_answer_is_correct(reveal_first):
    r,e,p=context_case();wrong=replace(p,out1_ppm=p.out1_ppm+1)
    first={**e['commit'],'data_hex':build_commitment(r,wrong.encode()).hex(),'structurally_valid':True}
    second={**e['commit'],'block_number':e['commit']['block_number']+1,'structurally_valid':True}
    calls=[second,first,{**e['reveal'],'structurally_valid':True}]
    if reveal_first:calls.append({**e['reveal'],'data_hex':wrong.encode().hex(),'structurally_valid':True})
    selected=choose(calls,r,e['snapshot']['hotkey'])
    assert selected['commit'] is first
    e.update(commit=selected['commit'],reveal=selected['reveal'])
    assert verify_shadow(r,e)['status']==('MISMATCH' if reveal_first else 'MISSING')


@pytest.mark.parametrize('field',['reported_netuid','mining_netuid','round_id','hash'])
def test_relabeling_an_envelope_cannot_reuse_the_reveal(field):
    r,e,p=context_case();raw=bytearray.fromhex(e['commit']['data_hex'])
    raw[{'reported_netuid':13,'mining_netuid':11,'round_id':15,'hash':47}[field]]^=1
    changed=bytes(raw);e['commit']['data_hex']=changed.hex()
    assert not matches_reveal(changed,p.encode())
    assert verify_shadow(r,e)['status']=='MALFORMED'
    calls=[{**e[leg],'structurally_valid':True} for leg in ('commit','reveal')]
    selected=choose(calls,r,e['snapshot']['hotkey'])
    assert selected['reveal'] is None
    assert (selected['commit'] is not None)==(field=='hash')


@pytest.mark.parametrize('bad',['',None,'context-v2',True])
def test_unknown_format_is_rejected_without_historical_fallback(bad):
    r,_,_=context_case();r['commitment_format']=bad
    with pytest.raises(ValueError,match='format'):validate_round(r)


def test_original_sha256_format_remains_original_first_commit_selection():
    r,e,p=report_case()
    first={**e['commit'],'structurally_valid':True}
    wrong=replace(p,netuid=7)
    first['data_hex']=commitment_of(wrong.encode()).hex()
    later={**e['commit'],'block_number':first['block_number']+1,'structurally_valid':True}
    reveal={**e['reveal'],'structurally_valid':True}
    other_reveal={**reveal,'data_hex':wrong.encode().hex(),'block_number':reveal['block_number']-1}
    selected=choose([later,reveal,first,other_reveal],r,e['snapshot']['hotkey'])
    assert selected['commit'] is first and selected['reveal'] is other_reveal
    e.update(commit=selected['commit'],reveal=selected['reveal'])
    assert verify_shadow(r,e)['reason']=='payload_context'
    # A new context envelope must not be auto-detected by a historical round.
    new={**r,'commitment_format':FORMAT};first['data_hex']=build_commitment(new,p.encode()).hex()
    selected=choose([first,reveal],r,e['snapshot']['hotkey'])
    assert selected['commit'] is None


def test_decode_collector_and_commitment_slot_accept_exact_bigraw_envelope():
    from noah.reports.backing.collector import _decode_block
    r,e,_=context_case();raw=bytes.fromhex(e['commit']['data_hex'])
    stored={'info':commitment_info('BigRaw',raw)}
    assert context_commitment_of(stored)==raw
    assert context_commitment_of([stored,stored]) is None
    assert context_commitment_of(commitment_info('BigRaw',raw[:-1])) is None
    tx=SimpleNamespace(value={'address':'0x'+e['snapshot']['hotkey'],'call':{
        'call_module':'Commitments','call_function':'set_commitment','call_args':{'netuid':82,'info':commitment_info('BigRaw',raw)}}})
    connection=SimpleNamespace(rpc_request=lambda *_:{'result':{}},get_block=lambda **_:{'extrinsics':[tx]},
        query=lambda *_args,**_kw:SimpleNamespace(value=[{'extrinsic_idx':0,'event_id':'ExtrinsicSuccess'}]))
    row=dict(hash='0x'+e['commit']['block_hash'],number=e['commit']['block_number'],timestamp_ms=e['commit']['timestamp_ms'],finality={},finalized=True)
    calls,_=_decode_block(connection,row)
    assert calls[0]['structurally_valid'] and calls[0]['successful'] and calls[0]['data_hex']==raw.hex()


def prepare_context(case):
    c,r,chain,engine=case;r.update(eligibility_policy_version=REPORT_POLICY,commitment_format=FORMAT)
    miner.prepare_report(c,r,compute_fn=lambda *_:engine)
    original=chain.broadcast
    def broadcast(handle):
        receipt=original(handle)
        if len(handle['payload'])==79:chain.stored=handle['payload']
        return receipt
    chain.broadcast=broadcast
    return c,r,chain,state_directory(c,r)/'prepared.json'


def test_context_sidecar_restart_commit_and_reveal_preserve_exact_bytes(case):
    c,r,chain,path=prepare_context(case);before=path.read_bytes();saved=load_prepared(path)
    assert json.loads(before)['schema']=='noah-report-prepare/2'
    assert len(saved.wire_commitment)==79 and saved.context_round==r
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
    chain.now=r['reveal_open_ms']
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
    assert miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)['action']=='complete'
    assert [tx['variant'] for tx in chain.sent]==['BigRaw','BigRaw']
    assert [len(tx['payload']) for tx in chain.sent]==[79,203]
    assert path.read_bytes()==before


def test_unknown_context_send_reconciles_without_signer_or_resend(case):
    c,r,chain,path=prepare_context(case);broadcast=chain.broadcast
    def uncertain(handle):broadcast(handle);raise ConnectionError('synthetic lost reply')
    chain.broadcast=uncertain
    with pytest.raises(SubmitError,match='unknown'):miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)
    chain.visible=False
    with pytest.raises(SubmitError,match='unknown'):miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    chain.visible=True
    result=miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    assert not result['sent'] and result['commit']=='finalized' and len(chain.sent)==1


@pytest.mark.parametrize('field',['context_round','commitment_envelope_hex','commitment_sha256','reveal_preimage_hex'])
def test_context_sidecar_tampering_is_refused(case,field):
    _,_,_,path=prepare_context(case);body=json.loads(path.read_bytes())
    if field=='context_round':body[field]['reveal_close_ms']+=1
    else:body[field]='00'+body[field][2:]
    path.write_text(json.dumps(body))
    with pytest.raises(PrepareError):load_prepared(path)


@pytest.mark.parametrize('change',['failed','unfinalized','other-signer','other-chain','call-target','malformed'])
def test_invalid_earlier_candidate_cannot_claim_the_context(change):
    r,e,_=context_case();good={**e['commit'],'structurally_valid':True}
    bad={**good,'block_number':good['block_number']-1}
    if change=='failed':bad['successful']=False
    elif change=='unfinalized':bad['finalized']=False
    elif change=='other-signer':bad['signer']='ff'*32
    elif change=='other-chain':bad['genesis_hash']='ff'*32
    elif change=='call-target':bad['target_netuid']=83
    else:bad['data_hex']='00'+bad['data_hex'][2:]
    reveal={**e['reveal'],'structurally_valid':True}
    selected=choose([bad,good,reveal],r,e['snapshot']['hotkey'])
    assert selected['commit'] is good and selected['reveal'] is reveal


def test_historical_sidecar_cannot_be_submitted_under_context_round_before_wallet(case):
    c,r,chain,engine=case;r['eligibility_policy_version']=REPORT_POLICY
    miner.prepare_report(c,r,compute_fn=lambda *_:engine)
    historical=(state_directory(c,r)/'prepared.json').read_bytes()
    r['commitment_format']=FORMAT
    miner.prepare_report(c,r,compute_fn=lambda *_:engine)
    (state_directory(c,r)/'prepared.json').write_bytes(historical)
    from noah.reports.common import ReportError
    with pytest.raises(ReportError,match='format or round'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert chain.sent==[]


def test_valid_sidecar_for_another_round_is_refused_before_wallet(case):
    c,r,chain,path=prepare_context(case)
    body=json.loads(path.read_bytes());other={**r,'reveal_close_ms':r['reveal_close_ms']+1}
    envelope=build_commitment(other,bytes.fromhex(body['reveal_preimage_hex']))
    body.update(context_round=other,commitment_envelope_hex=envelope.hex(),commitment_sha256=decode_commitment(envelope).report_hash.hex())
    path.write_text(json.dumps(body))
    assert load_prepared(path).context_round==other
    from noah.reports.common import ReportError
    with pytest.raises(ReportError,match='format or round'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert chain.sent==[]


def test_context_commitment_raw_scale_to_paired_adjudication_and_replay(tmp_path,monkeypatch):
    from noah.reports.tests import test_raw_export_integration as integration
    from noah.reports.tests.raw_export_fixture import RawChain, HOT, block_hash
    from noah.reports.backing.collector import _payload
    from substrateinterface import Keypair
    class ContextRawChain(RawChain):
        def __init__(self, *, context=False, unassigned=False, assigned_missing=False):
            super().__init__(context=context, unassigned=unassigned, assigned_missing=assigned_missing)
            self.contract['commitment_format']=FORMAT
            reveal=self.sdk.get_block(block_hash=block_hash(5))['extrinsics'][1].value['call']['call_args']
            info=next(item['value'] for item in reveal if item['name']=='info')
            envelope=build_commitment(self.contract,_payload(info))
            call=self.sdk.compose_call('Commitments','set_commitment',{'netuid':82,'info':commitment_info('BigRaw',envelope)})
            # Public key + supplied zero signature: encode only, no key access/signing.
            ext=self.sdk.create_signed_extrinsic(call,Keypair(public_key=HOT,crypto_type=1,ss58_format=42),nonce=0,signature=bytes(64))
            self.blocks[block_hash(3)]['block']['extrinsics'][1]=str(ext.data)
            key=self.sdk.create_storage_key('Commitments','CommitmentOf',[82,Keypair(public_key=HOT,crypto_type=1,ss58_format=42).ss58_address])
            stored=self.sdk.runtime_config.create_scale_object(key.value_scale_type,metadata=self.sdk.metadata)
            encoded=stored.encode({'deposit':0,'block':3,'info':commitment_info('BigRaw',envelope)})
            decoded=self.sdk.runtime_config.create_scale_object(key.value_scale_type,data=encoded,metadata=self.sdk.metadata).decode()
            assert context_commitment_of(decoded)==envelope
    monkeypatch.setattr(integration,'RawChain',ContextRawChain)
    fixture=integration.build(tmp_path/'context-raw-replay')
    assert fixture.result['status']=='COMPLETE'
    assert fixture.result['scored_decisions'][0]['outcome']=='verified'
    from noah.reports.adjudication import verify_adjudication
    replay=verify_adjudication(fixture.args.package,fixture.request,fixture.result['manifest_sha256'])
    assert replay['scored_decisions'][0]['outcome']=='verified'
