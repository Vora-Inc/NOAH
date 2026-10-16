"""The original proxy reservation can be repriced only before durable signing."""
import copy

import pytest

from noah.chain.sources import Refusal
from .test_purchase_proxy import rig, run, request


def hashless_attempt(rig):
    r,s,j,client,a,signer,loads=rig
    original=a.proxy_request
    def remote(step,operation):
        req=request(r,s,step,operation)
        if operation=='recover-pre-submit':
            req.update(journal_id=step['journal_id'],reservation_token=step['reservation_token'])
        return signer.execute(req)
    a.proxy_request=remote
    nonce=r.chain.get_account_next_index
    def fail_nonce(address):
        r.chain.get_account_next_index=nonce
        raise ConnectionError('synthetic pre-signing failure')
    r.chain.get_account_next_index=fail_nonce
    first=run(*rig)
    assert first['steps']['purchase']['status']=='uncertain'
    assert j.rows()[0]['status']=='uncertain'
    assert 'nonce' not in j.rows()[0] and 'extrinsic_hash' not in j.rows()[0]
    assert not r.chain.submitted
    for _ in range(s.quote_max_age_blocks+1):r.chain.advance()
    return first,original


def test_same_reservation_repriced_once_then_original_label_completes(rig):
    r,s,j,client,a,signer,loads=rig
    first,original=hashless_attempt(rig)
    before=copy.deepcopy(j.rows()[0]);ledger=list(a.ledger._db.execute('SELECT * FROM mixed_reservations'))
    recovered=run(*rig,recover_pre_submit=True)
    assert recovered['recovered_pre_submit'] and not recovered['signed'] and not recovered['broadcast']
    assert not r.chain.submitted and loads==['opened']
    current=j.rows()[0]
    assert current['status']=='not_submitted' and current['id']==before['id']
    assert current['journal_id']==before['journal_id']
    assert current['reservation_token']==before['reservation_token']
    assert current['request']['quote_block']>before['request']['quote_block']
    assert current['recovery_history'][0]['request']==before['request']
    assert len(list(a.ledger._db.execute('SELECT * FROM mixed_reservations')))==len(ledger)==1
    with pytest.raises(Refusal,match='different settings'):
        signer.execute(request(r,s,first['steps']['purchase'],'submit'))
    completed=run(*rig)
    assert completed['delivery_complete'] and len(r.chain.submitted)==3
    assert len(j.rows())==3
    a.proxy_request=original


@pytest.mark.parametrize('marker', ['nonce','extrinsic_hash','block_hash','events'])
def test_signed_or_observed_evidence_never_repriced(rig,marker):
    r,s,j,client,a,signer,loads=rig
    hashless_attempt(rig)
    row=j.rows()[0];row[marker]='evidence';j.put(row)
    with pytest.raises(Refusal,match='pre-submission'):
        run(*rig,recover_pre_submit=True)
    assert j.rows()[0][marker]=='evidence' and not r.chain.submitted


def test_public_recovery_flag_forwards_and_excludes_preview():
    from noah.deposits.assistant import miner_cli
    forwarded=[]
    assert miner_cli.main(['purchase-deposit','--label','fixed','--recover-pre-submit'],
                          transaction_runner=lambda args:forwarded.append(args) or 0)==0
    assert '--recover-pre-submit' in forwarded[0]
    with pytest.raises(SystemExit):
        miner_cli.main(['purchase-deposit','--label','fixed','--recover-pre-submit','--preview'])


def test_lost_recovery_reply_keeps_same_token_and_can_rebind_again(rig):
    r,s,j,client,a,signer,loads=rig
    hashless_attempt(rig)
    original=a.proxy_request
    def lost(step,operation):
        answer=original(step,operation)
        if operation=='recover-pre-submit':raise ConnectionError('synthetic lost recovery reply')
        return answer
    a.proxy_request=lost
    with pytest.raises(ConnectionError):run(*rig,recover_pre_submit=True)
    token=j.rows()[0]['reservation_token']
    assert j.rows()[0]['status']=='not_submitted'
    assert client.workflows()[0]['steps']['purchase']['status']=='uncertain'
    assert not r.chain.submitted
    a.proxy_request=original
    recovered=run(*rig,recover_pre_submit=True)
    assert recovered['recovered_pre_submit']
    assert j.rows()[0]['reservation_token']==token and not r.chain.submitted


@pytest.mark.parametrize('legacy_stage',[False,True])
def test_post_hash_quote_expiry_precedes_durable_submit_boundary(rig,legacy_stage):
    r,s,j,client,a,signer,loads=rig
    remote=a.proxy_request
    def recoverable(step,operation):
        req=request(r,s,step,operation)
        if operation=='recover-pre-submit':
            req.update(journal_id=step['journal_id'],reservation_token=step['reservation_token'])
        return signer.execute(req)
    a.proxy_request=recoverable
    original=r.chain.create_signed_extrinsic
    def age_after_sign(**kwargs):
        signed=original(**kwargs)
        for _ in range(s.quote_max_age_blocks+1):r.chain.advance()
        return signed
    r.chain.create_signed_extrinsic=age_after_sign
    out=run(*rig)
    row=j.rows()[0]
    assert out['steps']['purchase']['status']=='uncertain'
    assert row['extrinsic_hash'] and row['nonce']==0
    assert row['failure_stage']=='signed_before_submit' and row['failure_code']=='quote_expired'
    assert 'submit_started' not in row and not r.chain.submitted
    if legacy_stage:
        row['failure_stage']='after_hash';j.put(row)
        saved=client.workflows()[0];saved['steps']['purchase']['failure_stage']='after_hash';client.save(saved)
    old_hash,old_nonce=row['extrinsic_hash'],row['nonce']
    r.chain.create_signed_extrinsic=original
    recovered=run(*rig,recover_pre_submit=True)
    assert recovered['recovered_pre_submit'] and not r.chain.submitted
    history=j.rows()[0]['recovery_history']
    assert history[0]['extrinsic_hash']==old_hash and history[0]['nonce']==old_nonce
    assert j.rows()[0]['reservation_token']==row['reservation_token']
    assert 'extrinsic_hash' not in j.rows()[0] and 'nonce' not in j.rows()[0]
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3
    a.proxy_request=remote


def test_submit_rpc_entry_is_durable_and_blocks_recovery(rig):
    r,s,j,client,a,signer,loads=rig
    def fail_submit(*args,**kwargs):
        assert j.rows()[0]['submit_started'] is True
        raise ConnectionError('synthetic submission outcome unknown')
    r.chain.submit_extrinsic=fail_submit
    out=run(*rig)
    row=j.rows()[0]
    assert out['steps']['purchase']['status']=='uncertain'
    assert row['failure_stage']=='after_submit_started' and row['submit_started'] is True
    with pytest.raises(Refusal):run(*rig,recover_pre_submit=True)


def test_legacy_hashless_then_signed_expiry_keeps_both_attempts(rig):
    r,s,j,client,a,signer,loads=rig
    hashless_attempt(rig)
    assert run(*rig,recover_pre_submit=True)['recovered_pre_submit']
    original=r.chain.create_signed_extrinsic
    def expire_after_sign(**kwargs):
        signed=original(**kwargs)
        for _ in range(s.quote_max_age_blocks+1):r.chain.advance()
        return signed
    r.chain.create_signed_extrinsic=expire_after_sign
    assert run(*rig)['steps']['purchase']['failure_code']=='quote_expired'
    assert not r.chain.submitted
    r.chain.create_signed_extrinsic=original
    assert run(*rig,recover_pre_submit=True)['recovered_pre_submit']
    history=j.rows()[0]['recovery_history']
    assert len(history)==2 and history[1]['extrinsic_hash']
    assert len(client.workflows()[0]['attempt_history']['purchase'])>=2
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3
