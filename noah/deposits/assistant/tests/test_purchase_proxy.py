"""Synthetic proxy-chain tests; no wallet files, secrets or live transactions."""
from dataclasses import asdict, replace
import hashlib
import time
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase_runtime as R, purchase_workflow as W
from noah.deposits.assistant import purchase_signer as PS
from noah.deposits.assistant.proxy_signing import request_identity, validate_signing
from noah.deposits.assistant.signer import Journal, Signer
from noah.deposits.assistant.conversion_config import validate
from .fakes import COLD, MINER, VAL, OTHER_VAL, FakeKeypair
from .test_purchase import Chain, settings
from .test_session import Rig

DELEGATE=OTHER_VAL
SIGNING=dict(mode='proxy',proxy_address=DELEGATE,proxy_type='Transfer',purchase_proxy_type='Staking',
             delay_blocks=0,signer_ref='local-test:delegate',signer_socket='/tmp/noah-test-signer.sock')


def decode(call):
    params=call['call_params']
    return dict(call_module=call['call_module'],call_function=call['call_function'],
                call_args=[dict(name=k,value=decode(v) if k=='call' else v) for k,v in params.items()])


class ProxyChain(Chain):
    def __init__(self):
        super().__init__();self.delegations=[dict(delegate=DELEGATE,proxy_type=p,delay=0) for p in ('Staking','Transfer')]
        self.delegate_free=10**9;self.inner_failed=False;self.nonces=[]
    def query(self,module,name,params=None,block_hash=None):
        if module=='Proxy':return [self.delegations,0]
        if module=='System' and params[0]==DELEGATE:return {'data':{'free':self.delegate_free}}
        return super().query(module,name,params,block_hash)
    def create_signed_extrinsic(self,*,call,keypair,era=None,nonce=None):
        keypair.sign(bytearray(b'payload'));self.nonces.append(nonce)
        txhash='0x'+hashlib.sha256(repr((call,nonce)).encode()).hexdigest()
        return SimpleNamespace(value={'address':keypair.ss58_address,'call':decode(call)},
                               extrinsic_hash=txhash,call=call,delegate=keypair.ss58_address)
    def submit_extrinsic(self,signed,**kw):
        outer=signed.call;params=outer['call_params'];inner=params['call']
        # The actual inner origin is owner; it never becomes the delegate.
        assert params['real']==COLD and signed.delegate==DELEGATE
        assert params['force_proxy_type']==('Staking' if inner['call_function']=='add_stake_limit' else 'Transfer')
        direct=SimpleNamespace(signed={'call':inner,'signer':COLD},extrinsic_hash=signed.extrinsic_hash)
        if self.inner_failed:
            self.submitted.append(signed);self.advance();self.blocks[-1]={'events':[],'extrinsics':[signed]}
        else:
            super().submit_extrinsic(direct,**kw)
            self.submitted[-1]=signed;self.blocks[-1]['extrinsics']=[signed]
            self.blocks[-1]['events']=[e for e in self.blocks[-1]['events'] if e.get('module_id')!='System']
        self.blocks[-1]['events'] += [
            dict(extrinsic_idx=0,module_id='Proxy',event_id='ProxyExecuted',attributes={'result':{'Err':'CallFiltered'} if self.inner_failed else {'Ok':None}}),
            dict(extrinsic_idx=0,module_id='System',event_id='ExtrinsicSuccess',attributes=[])]


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'bittensor.sp_core',SimpleNamespace(
        Keypair=lambda ss58_address:SimpleNamespace(ss58_address=ss58_address)))
    r=Rig(tmp_path,chain=ProxyChain(),genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100,
          reserve_rao=0,signing=SIGNING)
    s=settings(plan_id='fixed-day-01');services=r.services()
    j=Journal(tmp_path/'delegate');client=W.PurchaseJournal(tmp_path/'client')
    loads=[]
    def loader(_):loads.append('opened');return FakeKeypair(DELEGATE)
    policy=dict(version=1,policy_id='miner01',runtime_code_hash=s.runtime_code_hash,spec_version=478,
                max_amount_rao=1000,max_total_rao=2000,max_fee_rao=s.max_fee_rao,
                conversion=validate(),purchase=asdict(s),max_total_tao_rao=10000)
    signer=Signer(r.config,services,policy,j,loader)
    a=R.Adapter(r.config,s,services)
    def remote(step,operation):return signer.execute(request(r,s,step,operation))
    a.proxy_request=remote
    yield r,s,j,client,a,signer,loads
    j.close();client.close()


def request(r,s,step,operation='submit'):
    action={'purchase':'purchase','deposit-noah':'purchase-noah','deposit-foreign':'purchase-foreign'}[step['action']]
    req=dict(version=1,expires_at=int(time.time())+120,operation=operation,action=action,
             intent_id=s.plan_id,amount_rao=step['amount_rao'],identity=request_identity(r.config))
    if operation in ('submit','status'):
        req.update(journal_id=step.get('journal_id'),reservation_token=step.get('reservation_token'))
    if action=='purchase':req.update(limit_price=step['params']['limit_price'],quote_block=step['quote_block'])
    return req


def run(r,s,j,client,a,signer,loads,**kw):
    return W.run(r.config,s,a,client,label='human-label',ask=lambda _:f'SEND PURCHASE {s.max_tao_rao} TAO RAO',say=lambda _:None,**kw)


def test_full_proxy_flow_exact_owner_attribution_and_nonce_sequence(rig):
    r,s,j,client,a,signer,loads=rig
    out=run(*rig)
    assert out['delivery_complete'],out
    assert r.chain.nonces==[0,1,2]
    assert loads==['opened']*3
    assert out['steps']['purchase']['purchase_result']['foreign_received_rao']==73
    assert out['steps']['deposit-foreign']['receipt']['amount_rao']==73
    assert out['steps']['deposit-foreign']['receipt']['origin_hotkey']==MINER
    assert out['steps']['deposit-foreign']['receipt']['coldkey']==COLD
    assert not out['credit_verified']
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3
    # Client label changes retain the same immutable plan and cannot buy again.
    other=W.run(r.config,s,a,client,label='another-label',ask=lambda _:pytest.fail('duplicate prompted'))
    assert other['delivery_complete'] and len(r.chain.submitted)==3


def test_preview_checks_both_delegations_without_secret(rig):
    *_,loads=rig
    assert run(*rig,preview=True)['preview']
    assert loads==[]


@pytest.mark.parametrize('kind',['missing_staking','missing_transfer','delayed','revoked','any_only'])
def test_bad_delegations_refuse_before_key_access(rig,kind):
    r,s,j,client,a,signer,loads=rig
    if kind=='missing_staking':r.chain.delegations=r.chain.delegations[1:]
    elif kind=='missing_transfer':r.chain.delegations=r.chain.delegations[:1]
    elif kind=='delayed':r.chain.delegations[0]['delay']=2
    elif kind=='revoked':r.chain.delegations=[]
    else:r.chain.delegations=[dict(delegate=DELEGATE,proxy_type='Any',delay=0)]
    with pytest.raises(Refusal):run(*rig,preview=True)
    assert not loads and not r.chain.submitted


def test_fees_charge_delegate_and_purchase_funds_are_real_owner(rig):
    r,s,j,client,a,signer,loads=rig
    r.chain.free=6000 #5000purchase+1000ED, no owner fees in proxy mode
    r.chain.delegate_free=1600 #three fees with2xheadroom+ED
    assert run(*rig,preview=True)['preview']
    r.chain.delegate_free=1599
    with pytest.raises(Refusal):run(*rig,preview=True)
    r.chain.delegate_free=10**9;r.chain.free=5999
    with pytest.raises(Refusal):run(*rig,preview=True)
    assert not loads


def test_proxy_inner_error_never_authorizes_delivery(rig):
    r,s,j,client,a,signer,loads=rig;r.chain.inner_failed=True
    out=run(*rig)
    assert out['steps']['purchase']['status']=='failed'
    assert not out['delivery_complete'] and len(r.chain.submitted)==1
    assert not run(*rig)['delivery_complete'] and len(r.chain.submitted)==1


def test_crash_after_signer_response_loss_recovers_without_hash_in_client(rig):
    r,s,j,client,a,signer,loads=rig
    remote=a.proxy_request
    def lost(step,operation):
        answer=remote(step,operation)
        if operation=='submit':raise ConnectionError('response lost')
        return answer
    a.proxy_request=lost
    out=run(*rig)
    assert out['steps']['purchase']['status']=='uncertain'
    assert 'extrinsic_hash' not in out['steps']['purchase']
    assert len(r.chain.submitted)==1
    a.proxy_request=remote
    out=run(*rig)
    assert out['delivery_complete'] and len(r.chain.submitted)==3


def test_signer_rejects_changed_plan_or_budget_and_arbitrary_payload(rig):
    r,s,j,client,a,signer,loads=rig
    quote=a.quote();step=a.prepare('purchase',5000,quote)
    req=request(r,s,step)
    for update in ({'intent_id':'different-day'},{'amount_rao':5001},{'call':{'anything':True}},
                   {'limit_price':quote['params']['limit_price']+1}):
        with pytest.raises(Refusal):signer.execute(dict(req,**update))
    assert not loads


def test_signer_does_not_trust_client_foreign_amount(rig):
    r,s,j,client,a,signer,loads=rig
    assert run(*rig)['delivery_complete']
    row=next(row for row in j.rows() if row['request']['action']=='purchase-foreign')
    req=dict(row['request'],expires_at=int(time.time())+120,operation='submit',amount_rao=74,
             journal_id=row['journal_id'],reservation_token=row['reservation_token'])
    with pytest.raises(Refusal):signer.execute(req)
    assert len(r.chain.submitted)==3


def test_common_delegate_journal_blocks_other_owner_work_and_caps_persist(rig):
    r,s,j,client,a,signer,loads=rig
    quote=a.quote();step=a.prepare('purchase',5000,quote);req=request(r,s,step)
    j.put(dict(id='other',genesis=r.config.genesis_hash,owner=VAL,delegate=DELEGATE,
               status='uncertain',miner=VAL,netuid=2,request={'action':'deposit','amount_rao':1}))
    with pytest.raises(Refusal):signer.execute(req)
    assert not loads
    j.db.execute('DELETE FROM intents')
    signer.policy['max_total_tao_rao']=4999
    with pytest.raises(Refusal):signer.execute(req)
    assert not loads


def test_signing_schema_has_explicit_staking_option_and_keeps_transfer_default():
    legacy={k:v for k,v in SIGNING.items() if k!='purchase_proxy_type'}
    assert validate_signing(legacy)==legacy
    assert validate_signing(SIGNING)==SIGNING
    with pytest.raises(Refusal):validate_signing(dict(SIGNING,purchase_proxy_type='Any'))


def test_signer_check_is_secret_free_and_rechecks_revocation(rig):
    r,s,j,client,a,signer,loads=rig
    quote=a.quote();step=a.prepare('purchase',5000,quote)
    assert signer.execute(request(r,s,step,'check'))['status']=='checked'
    assert not loads and j.rows()==[] and not r.chain.submitted
    r.chain.delegations=[]
    with pytest.raises(Refusal):signer.execute(request(r,s,step))
    assert not loads and not j.rows()


def test_wrong_delegate_key_and_post_unlock_revocation_never_broadcast(rig):
    r,s,j,client,a,signer,loads=rig
    signer.key_loader=lambda _:FakeKeypair(VAL)
    out=run(*rig)
    assert out['steps']['purchase']['status']=='failed'
    assert not r.chain.submitted


def test_post_unlock_revocation_leaves_uncertain_journal_without_submission(rig):
    r,s,j,client,a,signer,loads=rig
    def revoked(_):
        loads.append('opened');r.chain.delegations=[]
        return FakeKeypair(DELEGATE)
    signer.key_loader=revoked
    out=run(*rig)
    assert out['steps']['purchase']['status']=='uncertain'
    assert not r.chain.submitted and len(j.rows())==1


def test_proxy_lifetime_cap_cannot_reset_with_new_plan_or_policy_id(rig):
    r,s,j,client,a,signer,loads=rig
    assert run(*rig)['delivery_complete']
    newer=replace(s,plan_id='fixed-day-02')
    policy=dict(signer.policy,purchase=asdict(newer),policy_id='renamed',max_total_tao_rao=9999)
    other=Signer(r.config,signer.services,policy,j,signer.key_loader)
    b=R.Adapter(r.config,newer,signer.services);b.signer_owned=True
    step=b.prepare('purchase',5000,b.quote())
    with pytest.raises(Refusal):other.execute(request(r,newer,step))
    assert len(r.chain.submitted)==3


def test_purchase_policy_cannot_bypass_plan_with_legacy_deposit_or_conversion(rig):
    r,s,j,client,a,signer,loads=rig
    for action in ('deposit','convert'):
        with pytest.raises(Refusal):
            signer.execute(dict(version=1,expires_at=int(time.time())+120,operation='submit',
                action=action,intent_id='new-label',amount_rao=100,identity=request_identity(r.config)))
    assert not loads and not r.chain.submitted


def alias(address):
    from noah.chain.ss58 import public_key_of, _base58_text
    raw=bytes([2])+public_key_of(address)
    return _base58_text(raw+hashlib.blake2b(b'SS58PRE'+raw).digest()[:2])


@pytest.mark.parametrize('field',['coldkey','miner_hotkey'])
def test_address_prefix_alias_cannot_repeat_fixed_plan(rig,field):
    r,s,j,client,a,signer,loads=rig
    assert run(*rig)['delivery_complete']
    aliased=replace(r.config,**{field:alias(getattr(r.config,field))})
    other=Signer(aliased,signer.services,signer.policy,j,signer.key_loader)
    assert PS.ident(other,'purchase')==PS.ident(signer,'purchase')
    old=next(row for row in j.rows() if row['request']['action']=='purchase')
    req=dict(old['request'],identity=request_identity(aliased),expires_at=int(time.time())+120,operation='prepare')
    assert other.execute(req)['status']=='finalized'
    if field=='miner_hotkey':r.chain.owners[aliased.miner_hotkey]=COLD
    b=R.Adapter(aliased,s,signer.services)
    out=W.run(aliased,s,b,client,label='new-alias-label',ask=lambda _:pytest.fail('duplicate prompted'))
    assert out['delivery_complete'] and len(r.chain.submitted)==3


@pytest.mark.parametrize('which',['max_amount_rao','max_total_rao'])
def test_full_noah_capacity_checked_before_purchase_and_check(rig,which):
    r,s,j,client,a,signer,loads=rig
    step=a.prepare('purchase',5000,a.quote())
    signer.policy[which]=99
    for operation in ('check','prepare'):
        with pytest.raises(Refusal):signer.execute(request(r,s,step,operation))
    assert not loads and not r.chain.submitted and j.rows()==[]


def test_prior_noah_spending_checked_before_any_purchase(rig):
    r,s,j,client,a,signer,loads=rig
    step=a.prepare('purchase',5000,a.quote())
    signer.policy['max_total_rao']=150
    j.put(dict(id='past',genesis=r.config.genesis_hash,owner=COLD,delegate=DELEGATE,
               status='finalized',miner=MINER,netuid=2,request={'action':'deposit','amount_rao':51}))
    with pytest.raises(Refusal):signer.execute(request(r,s,step,'check'))
    assert not loads and not r.chain.submitted


def test_disconnected_before_prepare_recovers_without_broadcast(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def offline(step,operation):
        if operation=='prepare':raise ConnectionError('offline')
        return remote(step,operation)
    a.proxy_request=offline
    out=run(*rig)
    assert out['steps']['purchase']['status']=='not_submitted'
    assert not out['steps']['purchase']['submission_started']
    assert j.rows()==[] and not loads and not r.chain.submitted
    a.proxy_request=remote
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3


def test_disconnected_before_submit_uses_durable_never_submitted_reservation(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def disconnected(step,operation):
        if operation=='submit':raise ConnectionError('offline before request sent')
        return remote(step,operation)
    a.proxy_request=disconnected
    out=run(*rig)
    assert out['steps']['purchase']['status']=='uncertain'
    assert j.rows()[0]['status']=='not_submitted' and not loads and not r.chain.submitted
    a.proxy_request=remote
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3


def test_lost_prepare_response_reuses_same_reservation(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def lost(step,operation):
        answer=remote(step,operation)
        if operation=='prepare':raise ConnectionError('prepare response lost')
        return answer
    a.proxy_request=lost
    assert run(*rig)['steps']['purchase']['status']=='not_submitted'
    token=j.rows()[0]['reservation_token']
    assert not loads
    a.proxy_request=remote
    assert run(*rig)['delivery_complete']
    assert next(row for row in j.rows() if row['request']['action']=='purchase')['reservation_token']==token
    assert len(r.chain.submitted)==3


@pytest.mark.parametrize('loss',['row','incarnation','replacement'])
def test_missing_or_replaced_signer_state_never_authorizes_retry(rig,tmp_path,loss):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def disconnected(step,operation):
        if operation=='submit':raise ConnectionError('offline')
        return remote(step,operation)
    a.proxy_request=disconnected
    assert run(*rig)['steps']['purchase']['status']=='uncertain'
    replacement=None
    if loss=='row':j.db.execute('DELETE FROM intents')
    elif loss=='incarnation':j.db.execute('DELETE FROM journal_identity')
    else:
        replacement=Journal(tmp_path/'replacement');signer.journal=replacement
    a.proxy_request=remote
    try:
        with pytest.raises(Refusal):run(*rig)
        assert not loads and not r.chain.submitted
    finally:
        if replacement:replacement.close()


def test_durable_reservation_blocks_competitor_and_own_retry_does_not_double_count(rig):
    r,s,j,client,a,signer,loads=rig
    signer.policy['max_total_tao_rao']=5000
    step=a.prepare('purchase',5000,a.quote())
    reservation=signer.execute(request(r,s,step,'prepare'))
    assert reservation['status']=='not_submitted'
    newer=replace(s,plan_id='another-day')
    other=Signer(r.config,signer.services,dict(signer.policy,purchase=asdict(newer)),j,signer.key_loader)
    with pytest.raises(Refusal):other.execute(request(r,newer,step,'prepare'))
    step.update(reservation)
    assert signer.execute(request(r,s,step))['status']=='finalized'
    assert len(r.chain.submitted)==1


def test_legacy_prepared_without_hash_is_never_called_not_submitted(rig):
    r,s,j,client,a,signer,loads=rig
    step=a.prepare('purchase',5000,a.quote())
    reservation=signer.execute(request(r,s,step,'prepare'))
    row=j.rows()[0];row['status']='prepared';row.pop('reservation_token');row.pop('journal_id');j.put(row)
    out=signer.execute(request(r,s,step,'prepare'))
    assert out['status']=='uncertain'
    assert not loads and not r.chain.submitted


def test_legacy_raw_address_journal_identity_is_found_without_fork(rig):
    from noah.deposits.assistant.signer import digest
    r,s,j,client,a,signer,loads=rig
    assert run(*rig)['delivery_complete']
    row=next(row for row in j.rows() if row['request']['action']=='purchase')
    j.db.execute('DELETE FROM intents WHERE id=?',(row['id'],))
    row['id']=digest(['fixed-purchase',r.config.genesis_hash,COLD,MINER,s.plan_id,'purchase'])
    row['policy_binding']=digest({'config':asdict(r.config),'purchase':asdict(s)})
    row.pop('journal_id');row.pop('reservation_token');j.put(row)
    req=dict(row['request'],expires_at=int(time.time())+120,operation='prepare')
    assert signer.execute(req)['status']=='finalized'
    aliased=replace(r.config,coldkey=alias(COLD))
    other=Signer(aliased,signer.services,signer.policy,j,signer.key_loader)
    # Legacy binding lacks canonical source config: refuse the alias, never
    # reinterpret a different hash as permission to create a second intent.
    with pytest.raises(Refusal):other.execute(dict(req,identity=request_identity(aliased)))
    assert len(r.chain.submitted)==3 and len(j.rows())==3


def test_cli_preview_checks_trusted_noah_limit_without_reserving_or_loading_key(rig):
    r,s,j,client,a,signer,loads=rig
    signer.policy['max_amount_rao']=99
    with pytest.raises(Refusal):run(*rig,preview=True)
    assert j.rows()==[] and client.workflows()==[] and not loads and not r.chain.submitted


def test_cli_preview_signer_offline_is_blocked_without_intent(rig):
    r,s,j,client,a,signer,loads=rig
    a.proxy_request=lambda *args:(_ for _ in ()).throw(ConnectionError('offline'))
    with pytest.raises(Refusal):run(*rig,preview=True)
    assert j.rows()==[] and client.workflows()==[] and not loads


@pytest.mark.parametrize('boundary',['initial_step','before_prepare_rpc','saved_token','before_submit_rpc'])
def test_proxy_crash_at_each_client_pre_submission_save_recovers(rig,boundary):
    r,s,j,client,a,signer,loads=rig;save=client.save;crashed=[]
    def crash(row):
        save(row)
        step=row['steps'].get('purchase',{})
        target={
            'initial_step':step.get('phase')=='preparing',
            'before_prepare_rpc':step.get('phase')=='reserving' and not step.get('reservation_token'),
            'saved_token':bool(step.get('reservation_token')) and not step.get('proxy_request'),
            'before_submit_rpc':step.get('phase')=='proxy_request' and step.get('submission_started'),
        }[boundary]
        if target and not crashed:
            crashed.append(True);raise SystemExit('injected process crash')
    client.save=crash
    with pytest.raises(SystemExit):run(*rig)
    assert crashed and not loads and not r.chain.submitted
    client.save=save
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3


def test_proxy_crash_while_rebuilding_saved_reservation_keeps_status_request(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request;save=client.save
    def disconnected(step,operation):
        if operation=='submit':raise ConnectionError('offline before submit')
        return remote(step,operation)
    a.proxy_request=disconnected
    assert run(*rig)['steps']['purchase']['status']=='uncertain'
    a.proxy_request=remote;crashed=[]
    def crash(row):
        save(row);step=row['steps'].get('purchase',{})
        if step.get('phase')=='preparing' and step.get('reservation_token') and not crashed:
            crashed.append(True);raise SystemExit('retry preparation interrupted')
    client.save=crash
    with pytest.raises(SystemExit):run(*rig)
    assert not loads and not r.chain.submitted
    client.save=save
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3


def test_process_exit_after_signer_prepare_before_client_token_save_recovers(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def crash(step,operation):
        result=remote(step,operation)
        if operation=='prepare':raise SystemExit('prepare response never persisted')
        return result
    a.proxy_request=crash
    with pytest.raises(SystemExit):run(*rig)
    assert j.rows()[0]['status']=='not_submitted' and not loads and not r.chain.submitted
    token=j.rows()[0]['reservation_token']
    a.proxy_request=remote
    assert run(*rig)['delivery_complete'] and len(r.chain.submitted)==3
    assert next(row for row in j.rows() if row['request']['action']=='purchase')['reservation_token']==token


def test_legacy_client_prepared_without_protocol_marker_stays_uncertain(rig):
    r,s,j,client,a,signer,loads=rig;remote=a.proxy_request
    def offline(step,operation):
        if operation=='prepare':raise ConnectionError('offline')
        return remote(step,operation)
    a.proxy_request=offline
    run(*rig)
    row=client.workflows()[0];step=row['steps']['purchase']
    step.update(status='prepared',phase='signing');step.pop('submission_protocol')
    client.save(row)
    a.proxy_request=remote
    assert run(*rig)['steps']['purchase']['status']=='uncertain'
    assert not loads and not r.chain.submitted
