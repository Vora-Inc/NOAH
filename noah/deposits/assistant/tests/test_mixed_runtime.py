from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import mixed_runtime as R, mixed_workflow as W
from noah.deposits.assistant.amounts import DepositRefused
from .fakes import COLD, MINER, FakeChain, FakeKeypair
from .test_session import Rig
from .test_mixed import settings


class Extrinsic:
    def __init__(self,signed):
        self.signed=signed
        self.extrinsic_hash=hashlib.sha256(repr(signed).encode()).digest()
        c=signed['call']
        self.value={'address':signed['signer'],'call':{
            'call_module':c['call_module'],'call_function':c['call_function'],
            'call_args':[{'name':k,'value':v} for k,v in c['call_params'].items()]}}


class Call(dict):
    call_index = '075a'


class Chain(FakeChain):
    def __init__(self):
        super().__init__();self.runtime=SimpleNamespace(runtime_version=478,transaction_version=1)
        self.missing_swap_output=False
    def compose_call(self, **kw):return Call(super().compose_call(**kw))
    def get_metadata_error(self,*args,**kw):return SimpleNamespace(value={'index':82})
    def get_block_hash(self,n):
        return '0x'+'b'*64 if n==0 else super().get_block_hash(n)
    def get_block_number(self,h):
        return 0 if h=='0x'+'b'*64 else super().get_block_number(h)
    def rpc_request(self,method,params):
        return {'result':'0x'+'a'*64 if method=='state_getStorageHash' else {'specVersion':478,'transactionVersion':1}}
    def query(self,module,name,params,block_hash=None):
        if name=='NetworksAdded':return True
        if (module,name)==('System','Account'):return {'data':{'free':10**9}}
        return super().query(module,name,params,block_hash)
    def get_constant(self,module,name):return getattr(self,'min_swap',79) if name=='InitialMinStake' else 1000
    def get_payment_info(self,**kw):return {'partial_fee':100}
    def get_account_next_index(self,owner):return len(self.submitted)
    def runtime_call(self,api,name,params):
        if name=='current_alpha_price':return 2*10**9 if params[0]==2 else 10**9
        if name=='sim_swap_alpha_for_tao':return {'tao_amount':80}
        if name=='sim_swap_tao_for_alpha':return {'alpha_amount':80}
        raise AssertionError(name)
    def create_signed_extrinsic(self,*,call,keypair,era=None,nonce=None):
        keypair.sign(bytearray(b'payload'))
        return Extrinsic({'call':call,'signer':keypair.ss58_address,'nonce':nonce})
    def submit_extrinsic(self,signed,**kw):
        self.submitted.append(signed)
        call=signed.signed['call'];p=call['call_params']
        if call['call_function']=='swap_stake_limit':
            def event(name,attrs):
                return dict(extrinsic_idx=0,module_id='SubtensorModule',event_id=name,attributes=attrs)
            events=[event('StakeRemoved',[COLD,MINER,80,40,2,0]),
                    event('StakeAdded',[COLD,MINER,80,73,3,0]),
                    event('StakeSwapped',[COLD,MINER,2,3,80])]
            if getattr(self,'source_fee',0):
                events[0]['attributes'][3]-=self.source_fee
                events[0]['attributes'][5]=self.source_fee
            if self.missing_swap_output:events.pop(1)
        else:
            events,_=self._effects(signed.signed)
        if getattr(self,'fail_conversion',False):
            events=[dict(extrinsic_idx=0,module_id='System',event_id='ExtrinsicFailed',
                         attributes={'dispatch_error':{'Module':{'index':7,'error':'0x52000000'}}}),
                    dict(extrinsic_idx=0,module_id='TransactionPayment',event_id='TransactionFeePaid',
                         attributes={'who':COLD,'actual_fee':100,'tip':0})]
        else:
            events.append(dict(extrinsic_idx=0,module_id='System',event_id='ExtrinsicSuccess',attributes=[]))
        self.advance()
        self.blocks[-1]={'events':events,'extrinsics':[signed]}


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'bittensor.sp_core',SimpleNamespace(
        Keypair=lambda ss58_address:SimpleNamespace(ss58_address=ss58_address)))
    r=Rig(tmp_path,chain=Chain(),genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100,reserve_rao=0)
    s=replace(settings(),max_slippage_bp=1000)
    j=W.MixedJournal(tmp_path/'mixed')
    yield r,s,j,R.Adapter(r.config,s,r.services())
    j.db.close()


def run(r,s,j,a,**kw):
    return W.run(r.config,s,a,j,label='test',ask=lambda p:'SEND MIXED 0.0000001 NOAH',say=lambda _:None,**kw)


def test_sdk_adapter_exact_receipts_and_foreign_holdings_excluded(rig):
    r,s,j,a=rig
    result=run(r,s,j,a)
    assert result['delivery_complete']
    assert [e.signed['call']['call_params']['alpha_amount'] for e in r.chain.submitted]==[40,60,73]
    assert result['steps']['deposit-noah']['receipt']['token_netuid']==2
    foreign=result['steps']['deposit-foreign']['receipt']
    assert foreign['token_netuid']==3 and foreign['amount_rao']==73
    assert foreign['origin_hotkey']==MINER and foreign['coldkey']==COLD
    assert r.source.stake>73  # Existing stake never sets the foreign deposit amount.
    run(r,s,j,a)
    assert len(r.chain.submitted)==3


def test_real_adapter_preview_does_not_load_keys(rig):
    r,s,j,a=rig
    assert run(r,s,j,a,preview=True)['quote']['estimated_output_rao']==80
    assert r.signer_calls==0 and r.chain.submitted==[]


def test_missing_exact_conversion_output_holds_deposits(rig):
    r,s,j,a=rig;r.chain.missing_swap_output=True
    result=run(r,s,j,a)
    assert result['steps']['convert']['status']=='uncertain'
    assert len(r.chain.submitted)==1
    assert 'deposit-noah' not in result['steps']


def test_expired_quote_is_checked_after_wallet_unlock(rig):
    r,s,j,a=rig
    quote=a.quote({'contribution_budget_rao':100,'swap_budget_rao':40})
    step=dict(a.prepare('convert',40,quote),action='convert',amount_rao=40)
    def unlock():
        r.chain.advance(s.quote_max_age_blocks+1)
        return FakeKeypair()
    a.services.signer_factory=unlock
    with pytest.raises(DepositRefused):a.sign(step)
    assert r.chain.submitted==[]


def test_sdk_transaction_version_mismatch_refuses_signing(rig):
    r,s,j,a=rig
    quote=a.quote({'contribution_budget_rao':100,'swap_budget_rao':40})
    step=dict(a.prepare('convert',40,quote),action='convert',amount_rao=40)
    r.chain.runtime.transaction_version=999
    with pytest.raises(DepositRefused):a.sign(step)
    assert r.chain.submitted==[]


def test_open_direct_intent_blocks_mixed_before_key_or_broadcast(rig):
    from noah.deposits.assistant import ledger as L
    from noah.chain.sources import Refusal
    r,s,j,a=rig
    ident=r.ledger.create_intent(fingerprint='existing-direct',miner_hotkey=r.config.miner_hotkey,
        mode='fixed',amount_rao=100,shape='earnings')
    for state in (L.APPROVED,L.SUBMITTING,L.UNCERTAIN):r.ledger.transition(ident,state)
    with pytest.raises(Refusal,match='still open'):run(r,s,j,a)
    assert r.ledger.get(ident)['state']==L.UNCERTAIN
    assert r.signer_calls==0 and r.chain.submitted==[] and j.workflows()==[]


def test_incomplete_mixed_blocks_direct_and_other_journal(rig,tmp_path):
    from noah.chain.sources import Refusal
    r,s,j,a=rig;r.chain.missing_swap_output=True
    assert not run(r,s,j,a)['delivery_complete']
    count=len(r.chain.submitted)
    assert r.run()['status']=='refused'
    assert len(r.chain.submitted)==count
    other=W.MixedJournal(tmp_path/'alternate-journal')
    try:
        with pytest.raises(Refusal,match='original durable journal'):run(r,s,other,a)
    finally:other.db.close()
    assert len(r.chain.submitted)==count


def test_complete_mixed_releases_direct_guard(rig):
    r,s,j,a=rig
    assert run(r,s,j,a)['delivery_complete']
    r.ledger.check_direct_execution()


def test_orphan_canonical_reservation_refuses_after_crash(rig,monkeypatch):
    from noah.chain.sources import Refusal
    r,s,j,a=rig
    def crash(row):raise SystemExit('crash between canonical reservation and journal save')
    monkeypatch.setattr(j,'save',crash)
    with pytest.raises(SystemExit):run(r,s,j,a)
    assert j.workflows()==[] and r.chain.submitted==[]
    with pytest.raises(Refusal,match='original durable journal'):run(r,s,j,a)
    assert r.run()['status']=='refused'
    assert r.signer_calls==0


def _hold_command(directory, mixed, ready, release):
    from pathlib import Path
    from .test_mixed_workflow import Adapter as PureAdapter
    r=Rig(Path(directory),chain=Chain(),genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100,reserve_rao=0)
    def ask(_):
        ready.set()
        assert release.wait(10)
        return ''  # No transaction after the concurrency observation.
    if mixed:
        j=W.MixedJournal(Path(directory)/'child-mixed')
        a=PureAdapter(j);a.ledger=r.ledger
        W.run(r.config,settings(),a,j,label='child',ask=ask,say=lambda _:None)
        j.db.close()
    else:
        from noah.deposits.assistant.session import run_once
        services=r.services();services.ask=ask
        run_once(r.config,services,label='child')
    r.ledger.close()


@pytest.mark.parametrize('holding_mixed',[True,False])
def test_competing_commands_cannot_pass_check_before_other_reserves(rig,tmp_path,holding_mixed):
    import multiprocessing
    from noah.chain.sources import Refusal
    r,s,j,a=rig
    ctx=multiprocessing.get_context('fork')
    ready,release=ctx.Event(),ctx.Event()
    child=ctx.Process(target=_hold_command,args=(str(tmp_path),holding_mixed,ready,release))
    child.start()
    try:
        assert ready.wait(5),'child did not reach approval while holding canonical lock'
        if holding_mixed:
            result=r.run()
            assert result['status']=='refused' and 'Another deposit command' in result['reason']
        else:
            with pytest.raises(Refusal,match='Another deposit command'):run(r,s,j,a)
        assert r.signer_calls==0 and r.chain.submitted==[]
    finally:
        release.set();child.join(5)
        if child.is_alive():child.terminate();child.join()
    assert child.exitcode==0


def test_crash_after_final_delivery_before_reservation_release(rig,monkeypatch):
    r,s,j,a=rig
    original=r.ledger.complete_mixed
    def crash(ident):raise SystemExit('crash after all finalized receipts before release')
    monkeypatch.setattr(r.ledger,'complete_mixed',crash)
    with pytest.raises(SystemExit):run(r,s,j,a)
    assert len(r.chain.submitted)==3
    assert r.run()['status']=='refused'
    monkeypatch.setattr(r.ledger,'complete_mixed',original)
    assert run(r,s,j,a,status=True)['delivery_complete']
    r.ledger.check_direct_execution()
    assert len(r.chain.submitted)==3


@pytest.mark.parametrize('minimum,accepted',[(79,True),(80,False),(81,False)])
def test_swap_minimum_is_strict_tao_boundary_before_signing(rig,minimum,accepted):
    r,s,j,a=rig;r.chain.min_swap=minimum
    if accepted:
        assert run(r,s,j,a,preview=True)['quote']['minimum']=={
            'simulated_tao_rao':80,'strict_minimum_tao_rao':79}
    else:
        with pytest.raises(DepositRefused,match='runtime minimum'):run(r,s,j,a,preview=True)
    assert r.signer_calls==0 and r.chain.submitted==[]


def test_minimum_rechecked_after_wallet_unlock(rig):
    r,s,j,a=rig
    quote=a.quote({'contribution_budget_rao':100,'swap_budget_rao':40})
    step=dict(a.prepare('convert',40,quote),action='convert',amount_rao=40)
    def unlock():
        r.chain.min_swap=80
        return FakeKeypair()
    a.services.signer_factory=unlock
    with pytest.raises(DepositRefused,match='runtime minimum'):a.sign(step)
    assert r.chain.submitted==[]


def close_failed(r,s,j,a,phrase='CLOSE FAILED test'):
    return W.run(r.config,s,a,j,label='test',close_failed=True,ask=lambda _:phrase,say=lambda _:None)


def test_explicit_failed_closure_preserves_receipt_fee_and_never_retries(rig):
    r,s,j,a=rig;r.chain.fail_conversion=True
    failed=run(r,s,j,a)
    assert failed['steps']['convert']['status']=='failed'
    assert r.run()['status']=='refused'
    assert close_failed(r,s,j,a,phrase='no')['closure_declined']
    closed=close_failed(r,s,j,a)
    assert closed['closed_failed'] and not closed['delivery_complete']
    assert closed['failure_closure']['transaction_fees'][0]['actual_fee']==100
    assert closed['steps']==failed['steps']
    r.ledger.check_direct_execution()
    assert run(r,s,j,a)['closed_failed']
    assert len(r.chain.submitted)==1


def test_closure_crash_preserves_hold_until_status_recovery(rig,monkeypatch):
    r,s,j,a=rig;r.chain.fail_conversion=True
    run(r,s,j,a)
    original=r.ledger.close_failed_mixed
    def crash(_):raise SystemExit('after closed journal before canonical release')
    monkeypatch.setattr(r.ledger,'close_failed_mixed',crash)
    with pytest.raises(SystemExit):close_failed(r,s,j,a)
    assert r.run()['status']=='refused'
    monkeypatch.setattr(r.ledger,'close_failed_mixed',original)
    assert run(r,s,j,a,status=True)['closed_failed']
    r.ledger.check_direct_execution()
    assert len(r.chain.submitted)==1


@pytest.mark.parametrize('condition',['uncertain','successful','partial'])
def test_failure_closure_rejects_uncertain_or_successful_steps(rig,condition):
    r,s,j,a=rig
    if condition=='uncertain':r.chain.missing_swap_output=True
    result=run(r,s,j,a)
    if condition=='partial':
        row=j.workflows()[0];row['steps']['deposit-foreign']['status']='uncertain';j.save(row)
    with pytest.raises(DepositRefused):close_failed(r,s,j,a)
    assert len(r.chain.submitted)==(1 if condition=='uncertain' else 3)


@pytest.mark.parametrize('change',['movement','fee_payer','other_error','missing_fee'])
def test_failure_closure_requires_exact_known_early_error(rig,monkeypatch,change):
    r,s,j,a=rig;r.chain.fail_conversion=True
    result=run(r,s,j,a);step=result['steps']['convert']
    proof=a.reconcile(step)
    if change=='movement':proof['zero_stake_movements']=False
    if change=='fee_payer':proof['transaction_fees'][0]['who']=r.config.validator_hotkey
    if change=='other_error':proof['dispatch_failures'][0]['dispatch_error']['Module']['error']='0x53000000'
    if change=='missing_fee':proof['transaction_fees']=[]
    monkeypatch.setattr(a,'reconcile',lambda _:proof)
    with pytest.raises(DepositRefused):close_failed(r,s,j,a)
    assert len(r.chain.submitted)==1


def test_closed_failure_allows_new_reviewed_label_in_same_state(rig):
    r,s,j,a=rig;r.chain.fail_conversion=True
    failed=run(r,s,j,a);close_failed(r,s,j,a)
    r.chain.fail_conversion=False
    kwargs=dict(label='second-reviewed',ask=lambda _:'SEND MIXED 0.0000001 NOAH',say=lambda _:None)
    assert W.run(r.config,s,a,j,preview=True,**kwargs)['preview']
    assert len(r.chain.submitted)==1
    assert W.run(r.config,s,a,j,**kwargs)['delivery_complete']
    assert len(r.chain.submitted)==4
    original=run(r,s,j,a)
    assert original['closed_failed'] and original['steps']==failed['steps']
    assert len(r.chain.submitted)==4


@pytest.mark.parametrize('flags',[{'preview':True,'close_failed':True},
                                  {'status':True,'close_failed':True}])
def test_readonly_mode_cannot_close_failed_intent(rig,flags):
    from unittest.mock import patch
    r,s,j,a=rig
    with pytest.raises(DepositRefused):run(r,s,j,a,**flags)
    with patch('noah.deposits.assistant.cli.build_services',side_effect=AssertionError('no chain access')):
        with pytest.raises(DepositRefused):R.main('/nonexistent.ini',label='same',**flags)
    assert r.signer_calls==0 and j.workflows()==[]


def test_nonzero_source_fee_preserves_budget_and_reconciles_without_reswap(rig,monkeypatch):
    r,s,j,a=rig;r.chain.source_fee=3
    original=R.conversion_result
    def old_decoder(*args,**kwargs):raise DepositRefused('old decoder cannot reconcile fee')
    monkeypatch.setattr(R,'conversion_result',old_decoder)
    first=run(r,s,j,a)
    assert first['steps']['convert']['status']=='uncertain' and len(r.chain.submitted)==1
    tx_hash=first['steps']['convert']['extrinsic_hash']
    monkeypatch.setattr(R,'conversion_result',original)
    status=run(r,s,j,a,status=True)
    step=status['steps']['convert']
    assert step['status']=='finalized' and step['extrinsic_hash']==tx_hash
    assert step['conversion_result']['noah_spent_rao']==40
    assert step['conversion_result']['source_alpha_fee_rao']==3
    assert step['reconciliation_history'][-1]['previous_status']=='uncertain'
    assert len(r.chain.submitted)==1
    done=run(r,s,j,a)
    assert done['delivery_complete']
    assert [e.signed['call']['call_params']['alpha_amount'] for e in r.chain.submitted]==[40,60,73]


def test_quote_expires_at_confirmation_without_poisoning_owner(rig):
    r,s,j,a=rig
    def approve(_):
        r.chain.advance(s.quote_max_age_blocks+1)
        return 'SEND MIXED 0.0000001 NOAH'
    result=W.run(r.config,s,a,j,label='test',ask=approve,say=lambda _:None)
    step=result['steps']['convert']
    assert step['status']=='not_submitted' and step['phase']=='preparing'
    assert result['closed_failed'] and result['failure_closure']['code']=='quote_expired'
    assert r.chain.submitted==[] and r.signer_calls==0
    r.ledger.check_direct_execution()
    assert run(r,s,j,a)['closed_failed']  # Same label never sends later.
    assert r.chain.submitted==[]


def test_signed_quote_expiry_is_known_presubmit_and_retains_hash(rig,monkeypatch):
    r,s,j,a=rig; original=a.broadcast
    def delayed(signed, **kw):
        r.chain.advance(s.quote_max_age_blocks+1)
        return original(signed, **kw)
    monkeypatch.setattr(a,'broadcast',delayed)
    result=run(r,s,j,a); step=result['steps']['convert']
    assert step['status']=='not_submitted' and step['extrinsic_hash']
    assert step['signing_context']['era_period']==64
    assert step['phase']=='pre_submission_checks' and step['submission_started'] is False
    assert result['closed_failed'] and r.chain.submitted==[]
    r.ledger.check_direct_execution()
    assert run(r,s,j,a)['closed_failed']
    assert r.chain.submitted==[]


@pytest.mark.parametrize('error', ['rpc', 'typed_after_attempt'])
def test_submission_attempt_marker_precedes_transport_and_never_releases_unknown(rig,monkeypatch,error):
    from noah.deposits.assistant.mixed import NotSubmitted
    r,s,j,a=rig
    def ambiguous(*args,**kwargs):
        saved=j.workflows()[0]['steps']['convert']
        assert saved['submission_started'] and saved['phase']=='submitting'
        assert saved['extrinsic_hash']
        if error=='typed_after_attempt': raise NotSubmitted('quote_expired')
        raise RuntimeError('PRIVATE_PROVIDER_SENTINEL')
    monkeypatch.setattr(r.chain,'submit_extrinsic',ambiguous)
    result=run(r,s,j,a)
    assert result['steps']['convert']['status']=='uncertain' and not result['closed_failed']
    assert 'PRIVATE_PROVIDER_SENTINEL' not in str(j.workflows())
    with pytest.raises(Refusal): r.ledger.check_direct_execution()
    assert not run(r,s,j,a,status=True)['delivery_complete']


def test_later_presubmit_refusal_keeps_prior_delivery_and_resumes_only_unsent_leg(rig,monkeypatch):
    from noah.deposits.assistant.mixed import NotSubmitted
    r,s,j,a=rig; original=a.broadcast
    def refuse_second(signed, **kw):
        if signed[1]['action']=='deposit-noah': raise NotSubmitted('pre_submission_check_failed')
        return original(signed,**kw)
    monkeypatch.setattr(a,'broadcast',refuse_second)
    result=run(r,s,j,a)
    assert result['steps']['convert']['status']=='finalized'
    assert result['steps']['deposit-noah']['status']=='not_submitted'
    assert not result['closed_failed'] and len(r.chain.submitted)==1
    with pytest.raises(Refusal): r.ledger.check_direct_execution()
    monkeypatch.setattr(a,'broadcast',original)
    result=run(r,s,j,a)
    assert result['delivery_complete'] and len(r.chain.submitted)==3
    assert len(j.workflows()[0]['attempt_history']['deposit-noah'])==1
    r.ledger.check_direct_execution()


def test_presubmit_terminal_record_survives_crash_before_owner_release(rig,monkeypatch):
    from noah.deposits.assistant.mixed import NotSubmitted
    r,s,j,a=rig
    monkeypatch.setattr(a,'broadcast',lambda *args,**kwargs: (_ for _ in ()).throw(NotSubmitted('quote_expired')))
    original=r.ledger.close_failed_mixed
    monkeypatch.setattr(r.ledger,'close_failed_mixed',lambda _: (_ for _ in ()).throw(SystemExit('crash')))
    with pytest.raises(SystemExit): run(r,s,j,a)
    assert j.workflows()[0]['closed_failed'] and r.chain.submitted==[]
    with pytest.raises(Refusal): r.ledger.check_direct_execution()
    monkeypatch.setattr(r.ledger,'close_failed_mixed',original)
    assert run(r,s,j,a,status=True)['closed_failed']
    r.ledger.check_direct_execution()
    assert r.chain.submitted==[]


def test_readonly_fee_preparation_failure_closes_without_poisoning_owner(rig,monkeypatch):
    r,s,j,a=rig
    def approve(_):
        monkeypatch.setattr(r.chain,'get_payment_info',lambda **kw: {'partial_fee': s.max_fee_rao})
        return 'SEND MIXED 0.0000001 NOAH'
    result=W.run(r.config,s,a,j,label='test',ask=approve,say=lambda _:None)
    step=result['steps']['convert']
    assert step['status']=='not_submitted' and step['phase']=='preparing'
    assert step['error_code']=='preparation_check_failed'
    assert 'extrinsic_hash' not in step and result['closed_failed']
    assert r.chain.submitted==[] and r.signer_calls==0
    r.ledger.check_direct_execution()
    assert run(r,s,j,a,status=True)['closed_failed']


def test_failure_to_persist_submission_marker_never_enters_transport(rig,monkeypatch):
    r,s,j,a=rig; original=j.save
    def crash_before_marker(row):
        if row['steps'].get('convert',{}).get('phase')=='submitting':
            raise SystemExit('durable marker unavailable')
        return original(row)
    monkeypatch.setattr(j,'save',crash_before_marker)
    with pytest.raises(SystemExit): run(r,s,j,a)
    assert r.chain.submitted==[]
    saved=j.workflows()[0]['steps']['convert']
    assert saved['status']=='signed' and saved['submission_started'] is False
    monkeypatch.setattr(j,'save',original)
    result=run(r,s,j,a,status=True)
    assert result['steps']['convert']['status']=='uncertain'
    assert not result['closed_failed'] and r.chain.submitted==[]
    with pytest.raises(Refusal): r.ledger.check_direct_execution()
