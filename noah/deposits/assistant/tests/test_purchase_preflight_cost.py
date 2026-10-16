"""Bounded RPC work and fresh authority checks; not a live latency guarantee."""
import time
from collections import Counter

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase_runtime as R, purchase_signer as PS
from .fakes import COLD, MINER, VAL, FakeKeypair
from .test_purchase_proxy import rig, request, DELEGATE
from .test_purchase_root_adapter import root_rig


def prepared(rig):
    r,s,j,client,a,signer,loads=rig
    a.signer_owned=True
    step=a.prepare('purchase',s.max_tao_rao,a.quote())
    return step


def test_one_full_quote_without_nested_prepare_at_each_purchase_check(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig)
    counts=Counter()
    for name in ('quote','identity','root_check','enabled','available','funds'):
        original=getattr(R.Adapter,name)
        def wrapped(self,*args,_name=name,_original=original,**kwargs):
            counts[_name]+=1
            return _original(self,*args,**kwargs)
        monkeypatch.setattr(R.Adapter,name,wrapped)
    original_fee=r.chain.get_payment_info
    def payment(**kwargs):
        counts['payment']+=1
        return original_fee(**kwargs)
    monkeypatch.setattr(r.chain,'get_payment_info',payment)
    monkeypatch.setattr(R.Adapter,'prepare',lambda *_:pytest.fail('nested prepare duplicates checks'))
    assert signer.execute(request(r,s,step,'check'))['status']=='checked'
    assert counts==dict(quote=1,identity=1,root_check=1,enabled=2,available=1,funds=2,payment=4)
    assert not loads and not j.rows() and not r.chain.submitted


def test_exact_requested_limit_used_for_all_purchase_fee_estimates(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig)
    req=request(r,s,step,'check');req['limit_price']=1_000_000_000
    limits=[]
    def payment(*,call,**_):
        inner=call['call_params']['call']
        if inner['call_function']=='add_stake_limit':limits.append(inner['call_params']['limit_price'])
        return {'partial_fee':100}
    monkeypatch.setattr(r.chain,'get_payment_info',payment)
    assert signer.execute(req)['status']=='checked'
    assert limits==[req['limit_price'],req['limit_price']]
    assert not loads


def test_limit_above_fresh_slippage_bound_fails_before_fee_or_key(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig);req=request(r,s,step,'check');req['limit_price']+=1
    monkeypatch.setattr(r.chain,'get_payment_info',lambda **_:pytest.fail('fee requested on invalid limit'))
    with pytest.raises(Refusal,match='slippage'):
        signer.execute(req)
    assert not loads and not j.rows()


def test_quote_age_rechecked_after_final_fee_and_balance_reads(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig)
    original=r.chain.get_payment_info;seen=[]
    def payment(**kwargs):
        seen.append(1)
        if len(seen)==4:
            for _ in range(s.quote_max_age_blocks+1):r.chain.advance()
        return original(**kwargs)
    monkeypatch.setattr(r.chain,'get_payment_info',payment)
    with pytest.raises(Refusal,match='quote expired'):
        signer.execute(request(r,s,step,'prepare'))
    assert not loads and not j.rows() and not r.chain.submitted


def test_final_purchase_fee_change_counts_toward_complete_sequence_survival(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig);r.chain.delegate_free=1600
    seen=[]
    def payment(**_):
        seen.append(1)
        return {'partial_fee':101 if len(seen)==4 else 100}
    monkeypatch.setattr(r.chain,'get_payment_info',payment)
    with pytest.raises(Refusal,match='survival'):
        signer.execute(request(r,s,step,'check'))
    assert not loads and not j.rows()


@pytest.mark.parametrize('boundary',[1,2])
@pytest.mark.parametrize('change',['delegation','delegate_funds','owner_funds','owner','toggle','slippage','runtime','fee'])
def test_each_authority_boundary_observes_new_failure_without_broadcast(rig,monkeypatch,boundary,change):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig)
    step.update(signer.execute(request(r,s,step,'prepare')))
    key=FakeKeypair(DELEGATE)
    signer.key_loader=lambda _:key
    real_calls=PS.calls;seen=[]
    def mutate():
        if change=='delegation':r.chain.delegations=[]
        elif change=='delegate_funds':r.chain.delegate_free=0
        elif change=='owner_funds':r.chain.free=0
        elif change=='owner':r.chain.owners[MINER]=VAL
        elif change=='toggle':signer.services.source.transfer_toggle_value=False
        elif change=='slippage':
            original=r.chain.runtime_call
            monkeypatch.setattr(r.chain,'runtime_call',lambda api,name,params,**kw:
                1 if name=='current_alpha_price' else original(api,name,params,**kw))
        elif change=='runtime':
            original=r.chain.rpc_request
            monkeypatch.setattr(r.chain,'rpc_request',lambda method,params:
                {'result':'0x'+'c'*64} if method=='state_getStorageHash' else original(method,params))
        elif change=='fee':monkeypatch.setattr(r.chain,'get_payment_info',lambda **_:{'partial_fee':s.max_fee_rao})
    def checked(*args):
        seen.append(1)
        if len(seen)==boundary:mutate()
        return real_calls(*args)
    monkeypatch.setattr(PS,'calls',checked)
    # PREPARE is already durable. Both remaining checks are inside the
    # guarded signing/submission attempt and preserve an uncertain receipt.
    assert signer.execute(request(r,s,step))['status']=='uncertain'
    assert len(seen)==boundary
    assert key.signed==(1 if boundary==2 else 0)
    assert not r.chain.submitted


def test_expiry_during_final_signature_checks_prevents_key_sign(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig);step.update(signer.execute(request(r,s,step,'prepare')))
    key=FakeKeypair(DELEGATE);signer.key_loader=lambda _:key
    req=request(r,s,step);clock=[time.time()]
    monkeypatch.setattr(PS.time,'time',lambda:clock[0])
    original=PS.calls;seen=[]
    def checked(*args):
        result=original(*args);seen.append(1)
        if len(seen)==1:clock[0]=req['expires_at']
        return result
    monkeypatch.setattr(PS,'calls',checked)
    assert signer.execute(req)['status']=='uncertain'
    assert key.signed==0 and not r.chain.submitted


def test_success_checks_fresh_authority_at_signature_and_submission(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig);step.update(signer.execute(request(r,s,step,'prepare')))
    original=R.Adapter.quote;seen=[]
    def quote(self,**kwargs):
        seen.append(kwargs['limit_price'])
        return original(self,**kwargs)
    monkeypatch.setattr(R.Adapter,'quote',quote)
    assert signer.execute(request(r,s,step))['status']=='finalized'
    assert seen==[step['params']['limit_price']]*2
    assert len(r.chain.submitted)==1


@pytest.mark.parametrize('limit',[999_999_999,1_000_000_000,1_000_000_001])
def test_requested_root_limit_still_requires_exact_one_to_one(root_rig,limit):
    f=root_rig;f.a.signer_owned=True
    if limit==1_000_000_000:
        assert f.a.quote(limit_price=limit)['params']['limit_price']==limit
    else:
        with pytest.raises(Refusal):f.a.quote(limit_price=limit)
    assert not f.loads and not f.r.chain.submitted


def test_already_expired_quote_refuses_before_heavy_quote_reads(rig,monkeypatch):
    r,s,j,client,a,signer,loads=rig
    step=prepared(rig)
    for _ in range(s.quote_max_age_blocks+1):r.chain.advance()
    monkeypatch.setattr(R.Adapter,'quote',lambda *_args,**_kw:pytest.fail('known expired quote re-read'))
    with pytest.raises(Refusal,match='quote expired'):
        signer.execute(request(r,s,step,'prepare'))
    assert not loads and not j.rows()
