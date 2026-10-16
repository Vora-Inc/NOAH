"""Synthetic chain evidence; these tests are not a live runtime proof."""
import copy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase as P, purchase_runtime as R, purchase_workflow as W
from noah.deposits.assistant.mixed import NotSubmitted
from noah.deposits.assistant.tests.test_mixed_runtime import Chain as MixedChain
from .fakes import COLD, MINER, VAL, config
from .test_session import Rig


def settings(**kw):
    return replace(P.PurchaseSettings(3,VAL,5000,5,1000000,'0x'+'a'*64,478),**kw)


class Chain(MixedChain):
    free=1000000000
    bad_receipt=False
    fail_purchase=False
    def runtime_call(self,api,name,params,block_hash=None):
        return super().runtime_call(api,name,params)
    def query(self,module,name,params=None,block_hash=None):
        if (module,name)==('System','Account'):return {'data':{'free':self.free}}
        if (module,name)==('Timestamp','Now'):return 1700000000000
        return super().query(module,name,params,block_hash)
    def submit_extrinsic(self,signed,**kw):
        call=signed.signed['call']
        if call['call_function']!='add_stake_limit':return super().submit_extrinsic(signed,**kw)
        self.submitted.append(signed)
        p=call['call_params']
        events=[dict(extrinsic_idx=0,module_id='SubtensorModule',event_id='StakeAdded',
                     attributes=[COLD,MINER,p['amount_staked'],73,3,5])]
        if self.bad_receipt:events[0]['attributes'][4]=4
        if self.fail_purchase:events=[]
        events.append(dict(extrinsic_idx=0,module_id='System',
            event_id='ExtrinsicFailed' if self.fail_purchase else 'ExtrinsicSuccess',attributes=[]))
        self.advance();self.blocks[-1]={'events':events,'extrinsics':[signed]}


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'bittensor.sp_core',SimpleNamespace(
        Keypair=lambda ss58_address:SimpleNamespace(ss58_address=ss58_address)))
    r=Rig(tmp_path,chain=Chain(),genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100,reserve_rao=0)
    s=settings();j=W.PurchaseJournal(tmp_path/'purchase');a=R.Adapter(r.config,s,r.services())
    yield r,s,j,a
    j.close()


def run(r,s,j,a,**kw):
    return W.run(r.config,s,a,j,label='day-1',ask=lambda _:f'SEND PURCHASE {s.max_tao_rao} TAO RAO',say=lambda _:None,**kw)


def test_fixed_purchase_does_not_sell_noah_and_deposits_exact_arrival(rig):
    r,s,j,a=rig
    out=run(r,s,j,a)
    assert out['delivery_complete'],out
    assert not out['credit_verified']
    calls=[x.signed['call'] for x in r.chain.submitted]
    assert [c['call_function'] for c in calls]==['add_stake_limit','transfer_stake_and_hotkey','transfer_stake_and_hotkey']
    assert calls[0]['call_params']['amount_staked']==5000
    assert calls[0]['call_params']['limit_price']==1010000000
    assert calls[0]['call_params']['allow_partial'] is False
    assert calls[1]['call_params']['alpha_amount']==100
    assert calls[2]['call_params']['alpha_amount']==73  # not estimated80 or existing balance
    assert out['steps']['purchase']['purchase_result']['gross_tao_moved_rao']==5000
    assert out['steps']['purchase']['events'][0]['ordinal_in_block']==0
    assert out['steps']['purchase']['historical_owner']==COLD
    assert out['steps']['purchase']['block_timestamp_ms']==1700000000000
    assert run(r,s,j,a)['delivery_complete']
    assert len(r.chain.submitted)==3


def test_preview_never_accesses_key_or_writes_intent(rig):
    r,s,j,a=rig
    a.services.signer_factory=lambda:pytest.fail('preview opened a wallet')
    out=run(r,s,j,a,preview=True)
    assert out['preview'] and not out['signed'] and not out['broadcast']
    assert j.workflows()==[] and r.chain.submitted==[]


@pytest.mark.parametrize('free',[5000,6000,6599])
def test_positive_survival_and_all_fees_required_before_purchase(rig,free):
    r,s,j,a=rig;r.chain.free=free
    with pytest.raises(Refusal):run(r,s,j,a,preview=True)
    assert not r.chain.submitted and not j.workflows()


def test_exact_survival_balance_is_permitted(rig):
    r,s,j,a=rig;r.chain.free=6600 # purchase5000 + three fees*2*100 + ED1000
    assert run(r,s,j,a,preview=True)['preview']


@pytest.mark.parametrize('failure',['bad_receipt','fail_purchase'])
def test_purchase_failure_or_ambiguous_receipt_stops_all_transfers(rig,failure):
    r,s,j,a=rig;setattr(r.chain,failure,True)
    out=run(r,s,j,a)
    assert not out['delivery_complete'] and set(out['steps'])=={'purchase'}
    assert out['steps']['purchase']['status'] in ('failed','uncertain')
    if failure=='bad_receipt':
        # Read-only status can refuse malformed evidence. It must never retry.
        with pytest.raises(Refusal):run(r,s,j,a,status=True)
    else:assert not run(r,s,j,a,status=True)['delivery_complete']
    assert len(r.chain.submitted)==1
    with pytest.raises(Refusal):
        W.run(r.config,s,a,j,label='other',preview=True)


def test_zero_noah_leg_is_skipped(rig):
    r,s,j,a=rig;r.config=replace(r.config,amount_rao=0);a.config=r.config
    out=run(r,s,j,a)
    assert out['delivery_complete']
    assert len(r.chain.submitted)==2 and 'deposit-noah' not in out['steps']


def test_crash_after_broadcast_reconciles_before_retry(rig):
    r,s,j,a=rig;original=a.broadcast
    def interrupt(signed,*,before_submit):
        original(signed,before_submit=before_submit)
        raise ConnectionError('provider secret exception text must not persist')
    a.broadcast=interrupt
    out=run(r,s,j,a)
    assert out['steps']['purchase']['status']=='uncertain'
    assert 'provider secret' not in str(j.workflows())
    a.broadcast=original
    out=run(r,s,j,a)
    assert out['delivery_complete'] and len(r.chain.submitted)==3


def test_pre_rpc_expiry_records_no_send_without_rebuy(rig):
    r,s,j,a=rig;original=a.prepare
    def expired(*args):raise NotSubmitted('quote_expired')
    a.prepare=expired
    out=run(r,s,j,a)
    assert out['steps']['purchase']['status']=='not_submitted' and not r.chain.submitted
    a.prepare=original
    assert run(r,s,j,a)['delivery_complete']
    assert len(r.chain.submitted)==3


def test_attempted_submission_cannot_masquerade_as_not_sent(rig):
    r,s,j,a=rig
    def uncertain(signed,*,before_submit):
        before_submit();raise NotSubmitted('quote_expired')
    a.broadcast=uncertain
    out=run(r,s,j,a)
    assert out['steps']['purchase']['status']=='uncertain'
    assert not run(r,s,j,a)['delivery_complete'] and not r.chain.submitted


def test_changed_settings_or_missing_journal_refused(rig,tmp_path):
    r,s,j,a=rig
    run(r,s,j,a)
    with pytest.raises(Refusal):run(r,replace(s,max_tao_rao=5001),j,a)
    other=W.PurchaseJournal(tmp_path/'other')
    try:
        with pytest.raises(Refusal):run(r,s,other,a)
    finally:other.close()


def test_existing_open_direct_and_owner_lock_refuse(rig):
    r,s,j,a=rig
    with a.ledger.execution_lock():
        with pytest.raises(Refusal):run(r,s,j,a)
    assert not r.chain.submitted


@pytest.mark.parametrize('change',[{'mode':'percent'},{'stake_position':'validator'},
    {'network':'mainnet'},{'amount_rao':True},{'amount_rao':101,'cap_rao':100},
    {'signing':{'mode':'proxy'}}])
def test_unproved_or_dynamic_modes_refused(change):
    c=config(genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100)
    if 'signing' in change:
        change={'signing':{'mode':'proxy'}}
    with pytest.raises(Refusal):
        P.test_gate(replace(c,**change),settings())


def test_config_roundtrip_and_default_slippage():
    raw=asdict(settings());raw.pop('max_slippage_bp')
    assert P.from_mapping(raw).max_slippage_bp==100
    for change in ({'foreign_netuid':-1},{'max_slippage_bp':101},{'max_tao_rao':True},
                   {'max_tao_rao':0},{'test_only':False},{'other':1}):
        with pytest.raises(Refusal):P.from_mapping(dict(raw,**change))


def test_receipt_global_ordinal_and_other_extrinsics_ignored():
    event=lambda i,module,name,a:dict(extrinsic_idx=i,module_id=module,event_id=name,attributes=a)
    rows=[event(1,'SubtensorModule','StakeAdded',[COLD,MINER,5000,999999,3,0]),
          event(2,'SubtensorModule','StakeAdded',[COLD,MINER,5000,73,3,5]),
          event(2,'System','ExtrinsicSuccess',[])]
    kwargs=dict(coldkey=COLD,hotkey=MINER,netuid=3,requested_rao=5000,extrinsic_index=2)
    assert P.purchase_result(rows,**kwargs)['event_ordinal']==1
    for field,value in [(0,VAL),(1,VAL),(2,4994),(3,0),(4,4),(5,5001)]:
        bad=copy.deepcopy(rows);bad[1]['attributes'][field]=value
        with pytest.raises(Refusal):P.purchase_result(bad,**kwargs)


def test_unknown_payload_and_changed_destination_rejected_before_signing(rig):
    r,s,j,a=rig
    quote=a.quote();step=a.prepare('purchase',5000,quote)
    step['function']='transfer_all'
    with pytest.raises(Refusal):a.sign(step)
    step=a.prepare('deposit-noah',100,quote)
    step['params']['destination_coldkey']=VAL
    with pytest.raises(Refusal):a.sign(step)
    assert not r.chain.submitted


def test_balance_or_noah_change_after_quote_blocks_signing(rig):
    r,s,j,a=rig
    quote=a.quote();step=a.prepare('purchase',5000,quote)
    r.chain.free=5000
    with pytest.raises(Refusal):a.sign(step)
    assert not r.chain.submitted


def test_failed_later_transfer_does_not_repeat_purchase(rig):
    r,s,j,a=rig;original=a.prepare
    def unavailable(action,*args):
        if action=='deposit-foreign':raise Refusal('temporarily unavailable')
        return original(action,*args)
    a.prepare=unavailable
    out=run(r,s,j,a)
    assert out['steps']['deposit-foreign']['status']=='not_submitted'
    assert len(r.chain.submitted)==2
    a.prepare=original
    out=run(r,s,j,a)
    assert out['delivery_complete'] and len(r.chain.submitted)==3


def test_purchase_ini_validation_and_installed_cli(tmp_path,capsys):
    from pathlib import Path
    from noah.deposits.assistant import miner_cli, config_edit
    from .test_cli import write
    path=Path(write(tmp_path,'fixed'))
    text=path.read_text().replace('[miner]','[miner]\nnetwork = private-test\ngenesis_hash = 0x'+'b'*64).replace('mode = fixed','mode = fixed\namount_noah = 0.0000001')
    text += '\n[purchase]\n'+'\n'.join(f'{k} = {"true" if v is True else v}' for k,v in asdict(settings()).items())+'\n'
    path.write_text(text)
    assert config_edit.validate_ini(path)
    assert miner_cli.main(['--config',str(path),'check-config'])==0
    assert 'valid' in capsys.readouterr().out
    assert miner_cli.main(['config','--json','--config',str(path)])==0
    import json
    assert json.loads(capsys.readouterr().out)['purchase']['max_tao_rao']==5000
    forwarded=[]
    assert miner_cli.main(['purchase-deposit','--config',str(path),'--label','example','--preview'],
                          transaction_runner=lambda args:forwarded.append(args) or 0)==0
    assert forwarded==[['--config',str(path),'purchase-deposit','--label','example','--preview']]
    path.write_text(text+'\n[mixed]\nforeign_netuid = 4\n')
    with pytest.raises(Refusal):config_edit.validate_ini(path)
