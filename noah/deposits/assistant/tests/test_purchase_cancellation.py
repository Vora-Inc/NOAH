"""Never-PREPARE cancellation uses durable signer proof, no chain or key calls."""
import copy
from dataclasses import asdict, replace
import time

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase_signer as PS, purchase_workflow as W
from noah.deposits.assistant.proxy_signing import request_identity
from noah.deposits.assistant.signer import Journal, Signer
from .test_purchase_proxy import rig, run, request
from .test_purchase_quote_refresh import expire_first_attempt, retry


def wire(rig):
    r,s,j,client,a,signer,loads=rig
    def cancel(operation, fields):
        req=dict(version=1,expires_at=int(time.time())+120,operation=operation,action='purchase',
                 intent_id=s.plan_id,amount_rao=s.max_tao_rao,identity=request_identity(r.config),
                 config_binding=fields['config_binding'])
        if operation=='cancel':
            req.update({k:fields[k] for k in ('journal_id','policy_fingerprint','cancellation_token')})
        return signer.execute(req)
    a.cancel_proxy=cancel
    return cancel


def no_chain(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    class Forbidden:
        def __getattr__(self,name):pytest.fail('chain access: '+name)
    a.connection=signer.services.connection=Forbidden()
    monkeypatch.setattr(a,'identity',lambda:pytest.fail('identity chain lookup'))
    monkeypatch.setattr(a,'quote',lambda:pytest.fail('quote chain lookup'))
    monkeypatch.setattr(signer,'runtime',lambda **k:pytest.fail('runtime lookup'))
    monkeypatch.setattr(signer,'key_loader',lambda _:pytest.fail('key load'))


def test_cancel_permanent_idempotent_and_no_chain(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    original=expire_first_attempt(rig);wire(rig);no_chain(rig,monkeypatch)
    out=retry(rig,cancel_unsubmitted=True)
    assert out['closed_failed'] and out['failure_closure']['status']=='cancelled'
    assert not out['delivery_complete'] and not out['credit_verified']
    assert client.workflows()[0]['steps']==original['steps']
    assert client.workflows()[0]['quote']==original['quote']
    assert not j.rows() and not loads and not r.chain.submitted
    assert a.ledger._db.execute('SELECT status FROM mixed_reservations').fetchone()[0]=='closed_failed'
    assert retry(rig,cancel_unsubmitted=True)==out
    assert retry(rig)==out
    assert retry(rig,preview=True)==out
    assert retry(rig,status=True)==out
    for action in ('purchase','deposit-noah','deposit-foreign'):
        for op in ('check','prepare','submit','status'):
            step=dict(action=action,amount_rao=s.max_tao_rao,params={'limit_price':123},quote_block=1)
            with pytest.raises(Refusal,match='permanently cancelled'):
                signer.execute(request(r,s,step,op))


@pytest.mark.parametrize('boundary',['ready_saved','cancel_lost_reply','closed_saved','before_cancel'])
def test_crash_boundaries_recover_same_closure(rig,monkeypatch,boundary):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);remote=wire(rig);no_chain(rig,monkeypatch)
    save=client.save
    def interrupted_save(row):
        save(row)
        if ((boundary=='ready_saved' and row.get('cancellation') and not row.get('closed_failed'))
                or (boundary=='closed_saved' and row.get('closed_failed'))):
            raise SystemExit('crash after fsync')
    def interrupted_rpc(op,fields):
        if op=='cancel' and boundary=='before_cancel':raise SystemExit('before RPC')
        out=remote(op,fields)
        if op=='cancel' and boundary=='cancel_lost_reply':raise ConnectionError('lost reply')
        return out
    monkeypatch.setattr(client,'save',interrupted_save)
    a.cancel_proxy=interrupted_rpc
    with pytest.raises((SystemExit,ConnectionError)):retry(rig,cancel_unsubmitted=True)
    assert a.ledger._db.execute('SELECT status FROM mixed_reservations').fetchone()[0]=='open'
    if boundary!='closed_saved':
        with pytest.raises(Refusal,match='cancellation is pending'):retry(rig)
        assert retry(rig,status=True)['cancellation_pending']
    saved=j.db.execute('SELECT body FROM purchase_cancellations').fetchone()
    monkeypatch.setattr(client,'save',save);a.cancel_proxy=remote
    out=retry(rig,cancel_unsubmitted=True)
    assert out['closed_failed'] and not loads and not r.chain.submitted
    if saved:assert j.db.execute('SELECT body FROM purchase_cancellations').fetchone()==saved


@pytest.mark.parametrize('status',['not_submitted','prepared','uncertain','finalized','refused','failed'])
@pytest.mark.parametrize('action',['purchase','purchase-noah','purchase-foreign','convert'])
def test_any_existing_matching_intent_refuses(rig,status,action):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);wire(rig)
    j.put(dict(id='existing',genesis=r.config.genesis_hash,owner=r.config.coldkey,miner=r.config.miner_hotkey,
               request=dict(intent_id=s.plan_id,action=action),status=status))
    with pytest.raises(Refusal,match='already holds an intent'):retry(rig,cancel_unsubmitted=True)
    assert not client.workflows()[0].get('closed_failed') and not loads and not r.chain.submitted


@pytest.mark.parametrize('marker',['extrinsic_hash','reservation_token','journal_id','proxy_request'])
def test_even_falsey_submission_evidence_refuses(rig,marker):
    row=expire_first_attempt(rig);wire(rig)
    row['steps']['purchase'][marker]=None;rig[3].save(row)
    with pytest.raises(Refusal,match='pre-PREPARE'):retry(rig,cancel_unsubmitted=True)


@pytest.mark.parametrize('change',[
    lambda s:s.pop('submission_protocol'),lambda s:s.update(phase='reserving'),
    lambda s:s.update(submission_started=True),lambda s:s.update(status='uncertain')])
def test_unsafe_history_refuses(rig,change):
    row=expire_first_attempt(rig);wire(rig)
    old=copy.deepcopy(row['steps']['purchase']);change(old)
    row['attempt_history']={'purchase':[old]};rig[3].save(row)
    with pytest.raises(Refusal,match='pre-PREPARE'):retry(rig,cancel_unsubmitted=True)


@pytest.mark.parametrize('error_code', [None, 'preparation_check_failed', 'signer_prepare_unavailable'])
def test_nonexpired_pre_prepare_attempt_refuses(rig,error_code):
    row=expire_first_attempt(rig);wire(rig)
    if error_code is None:
        row['steps']['purchase'].pop('error_code')
    else:
        row['steps']['purchase']['error_code']=error_code
    rig[3].save(row)
    with pytest.raises(Refusal,match='quote-expired'):
        retry(rig,cancel_unsubmitted=True)


@pytest.mark.parametrize('changed',['journal','policy','identity','config'])
def test_replacement_after_lost_reply_fails_closed(rig,monkeypatch,tmp_path,changed):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);remote=wire(rig)
    def lose(op,fields):
        result=remote(op,fields)
        if op=='cancel':raise ConnectionError('lost')
        return result
    a.cancel_proxy=lose
    with pytest.raises(ConnectionError):retry(rig,cancel_unsubmitted=True)
    a.cancel_proxy=remote
    replacement=None
    if changed=='journal':
        replacement=Journal(tmp_path/'replacement');signer.journal=replacement
    elif changed=='policy':signer.policy['max_total_tao_rao']+=1
    elif changed=='identity':signer.config=replace(signer.config,miner_hotkey=signer.config.validator_hotkey)
    else:signer.purchase=replace(signer.purchase,quote_max_age_blocks=99)
    try:
        with pytest.raises(Refusal):retry(rig,cancel_unsubmitted=True)
        assert not client.workflows()[0].get('closed_failed')
        assert a.ledger._db.execute('SELECT status FROM mixed_reservations').fetchone()[0]=='open'
        assert not loads and not r.chain.submitted
    finally:
        if replacement:replacement.close()


def test_cancelled_old_plan_cannot_change_caps_but_new_plan_can_start(rig):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);wire(rig);retry(rig,cancel_unsubmitted=True)
    old_policy=copy.deepcopy(signer.policy)
    signer.policy['max_total_tao_rao']+=100
    with pytest.raises(Refusal,match='permanently cancelled'):
        signer.execute(request(r,s,dict(action='purchase',amount_rao=s.max_tao_rao,params={'limit_price':123},quote_block=1),'check'))
    s2=replace(s,plan_id='reviewed-new-plan')
    signer.purchase=s2;signer.policy=old_policy;signer.policy['purchase']=asdict(s2);a.settings=s2
    a.proxy_request=lambda step,op:signer.execute(request(r,s2,step,op))
    result=W.run(r.config,s2,a,client,label='new',ask=lambda _:f'SEND PURCHASE {s2.max_tao_rao} TAO RAO',say=lambda _:None)
    assert result['delivery_complete'] and len(loads)==len(r.chain.submitted)==3
    assert signer.policy['max_total_tao_rao']==old_policy['max_total_tao_rao']


def test_missing_initialized_tombstone_table_refuses(rig):
    expire_first_attempt(rig);wire(rig);retry(rig,cancel_unsubmitted=True)
    rig[2].db.execute('DROP TABLE purchase_cancellations')
    with pytest.raises(Refusal,match='missing or incompatible'):
        rig[5].execute(request(rig[0],rig[1],dict(action='purchase',amount_rao=5000,params={'limit_price':123},quote_block=1),'check'))


def test_public_flag_forwarded_and_exclusive():
    from noah.deposits.assistant import miner_cli
    forwarded=[]
    assert miner_cli.main(['--config','original.ini','purchase-deposit','--label','old','--cancel-unsubmitted'],
                          transaction_runner=lambda args:forwarded.append(args) or 0)==0
    assert forwarded==[['--config','original.ini','purchase-deposit','--label','old','--cancel-unsubmitted']]
    with pytest.raises(SystemExit) as exc:
        miner_cli.main(['purchase-deposit','--label','old','--cancel-unsubmitted','--preview'])

    assert exc.value.code == 2


def test_reservation_between_handshake_and_cancel_refuses(rig):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);remote=wire(rig)
    def race(op,fields):
        if op=='cancel':
            j.put(dict(id='raced',genesis=r.config.genesis_hash,owner=r.config.coldkey,
                       miner=r.config.miner_hotkey,status='not_submitted',
                       request=dict(intent_id=s.plan_id,action='purchase')))
        return remote(op,fields)
    a.cancel_proxy=race
    with pytest.raises(Refusal,match='already holds an intent'):retry(rig,cancel_unsubmitted=True)
    assert not client.workflows()[0].get('closed_failed')
    assert a.ledger._db.execute('SELECT status FROM mixed_reservations').fetchone()[0]=='open'
    assert not loads and not r.chain.submitted


def test_runtime_cancel_never_builds_chain_services(rig,monkeypatch,capsys):
    import json
    from noah.deposits.assistant import cli, config, ledger, purchase, purchase_runtime
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);remote=wire(rig);no_chain(rig,monkeypatch)
    monkeypatch.setattr(cli,'build_services',lambda _:pytest.fail('opened chain services'))
    monkeypatch.setattr(config,'load_config',lambda _:r.config)
    monkeypatch.setattr(purchase,'load_settings',lambda _:s)
    monkeypatch.setattr(ledger,'Ledger',lambda _:r.ledger)
    monkeypatch.setattr(purchase_runtime.Adapter,'cancel_proxy',lambda self,op,fields:remote(op,fields))
    assert purchase_runtime.main('original.ini',label='human-label',journal_path=client.path,
                                 cancel_unsubmitted=True)==0
    assert json.loads(capsys.readouterr().out)['closed_failed']
    assert not loads and not r.chain.submitted


@pytest.mark.parametrize('field',['amount_rao','config_binding','identity'])
def test_typed_cancellation_mismatch_refuses(rig,field):
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig)
    req=dict(version=1,expires_at=int(time.time())+120,operation='cancel-check',action='purchase',
             intent_id=s.plan_id,amount_rao=s.max_tao_rao,identity=request_identity(r.config),
             config_binding=PS.binding(signer))
    if field=='amount_rao':req[field]+=1
    elif field=='config_binding':req[field]='f'*64
    else:req[field]=dict(req[field],delegate=r.config.coldkey)
    with pytest.raises(Refusal):signer.execute(req)
    assert not loads and not r.chain.submitted


def test_real_socket_payload_contract_without_chain(rig,monkeypatch):
    import io
    import json
    from noah.deposits.assistant import proxy_signing
    r,s,j,client,a,signer,loads=rig
    expire_first_attempt(rig);no_chain(rig,monkeypatch)
    payloads=[]
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,value):pass
        def connect(self,path):assert path==r.config.signing['signer_socket']
        def sendall(self,raw):
            payload=json.loads(raw);payloads.append(payload)
            assert not {'quote_block','limit_price','reservation_token'} & payload.keys()
            self.response=signer.execute(payload)
        def makefile(self,mode):return io.BytesIO(json.dumps(self.response).encode()+b'\n')
    monkeypatch.setattr(proxy_signing.socket,'socket',lambda *args:Socket())
    assert retry(rig,cancel_unsubmitted=True)['closed_failed']
    assert [p['operation'] for p in payloads]==['cancel-check','cancel']
    assert payloads[1]['journal_id']==client.workflows()[0]['failure_closure']['journal_id']
    assert not loads and not r.chain.submitted
