"""Real Store/RuntimePolicy scalar integration with synthetic paired snapshot inputs."""
import copy
import json
from types import SimpleNamespace

import pytest

from noah.chain.sources import SourceDisagreement, Unverified
from noah.core.calculation_record import encoded
from noah.points import ema_policy
from noah.points.ema_reference import EmaReference, credit, digest, validate_reference_proof, validate_fixture
from noah.points.run import agreed, typescript_text
from noah.points.runtime import RuntimePolicy
from noah.points.tests.test_amendment_eligibility import Chain, h, raw, financial_fixture, runtime_policy, collector


class EmaChain(Chain):
    price_fault=None
    def ema_price_snapshot(self, block, assets, *, expected_block_hash, ema_source):
        assert expected_block_hash==h(block) and ema_source=='SubnetMovingPrice'
        self.calls['ema_snapshot']+=1
        if self.price_fault:raise self.price_fault
        context=self.token_reference_context(block,assets)
        context['prices']={str(a):{'ema':{'raw':raw(2**31 if a==2 else 2**30,16)},
                                  'spot':{'raw':raw(500000000 if a==2 else 300000000,8)}} for a in assets}
        return context


def proof(tmp_path, chain=None, asset=3):
    chain=chain or EmaChain();c=EmaReference(chain,tmp_path,2,'SubnetMovingPrice')
    return c,c.result(50,asset)['document']


def test_parent_snapshot_group_shares_many_receipts_and_restart_reauthenticates(tmp_path):
    chain=EmaChain();c=EmaReference(chain,tmp_path,2,'SubnetMovingPrice')
    c.precollect([(50,3)]*100+[(50,4)]*100+[(50,0)]*100)
    for _ in range(100):
        assert credit(c.result(50,3)['document'],7)==3
    assert chain.calls['ema_snapshot']==1
    assert all(k[1] in (49,50) for k in chain.calls if isinstance(k,tuple) and k[0]=='life')
    assert len(c.documents)==3
    restarted=EmaReference(chain,tmp_path,2,'SubnetMovingPrice');restarted.result(50,3)
    assert chain.calls['ema_snapshot']==2


@pytest.mark.parametrize('asset',[0,3])
def test_supported_parent_raw_reference_roundtrip(tmp_path,asset):
    _,p=proof(tmp_path,asset=asset)
    assert validate_reference_proof(json.loads(encoded(p)))==digest(p)
    assert p['reference']=={'block':49,'block_hash':h(49)}
    assert credit(p,10)==(20 if asset==0 else 5)


def test_newborn_parent_absence_terminal_and_no_price_query(tmp_path):
    chain=EmaChain()
    chain.change=lambda b,a,v:v.update(NetworksAdded=(None,1),NetworkRegisteredAt=(None,8),RegisteredSubnetCounter=(None,8)) if b==49 and a==4 else None
    c,p=proof(tmp_path,chain,4)
    assert p['status']=='unsupported' and p['reason']=='reference-subnet-absent'
    assert credit(p,10)==0 and chain.calls['ema_snapshot']==0


def test_same_block_reuse_unknown_and_absence_cannot_mask_it(tmp_path):
    chain=EmaChain()
    def change(b,a,v):
        if b==49 and a==2:v['NetworksAdded']=(None,1)
        if b==50 and a==3:v['RegisteredSubnetCounter']=(2,8)
    chain.change=change
    with pytest.raises(Unverified):proof(tmp_path,chain)


@pytest.mark.parametrize('fault',[SourceDisagreement('different'),Unverified('unknown')])
def test_failed_reader_never_creates_a_terminal_proof(tmp_path,fault):
    chain=EmaChain();chain.price_fault=fault
    with pytest.raises(type(fault)):proof(tmp_path,chain)
    assert not list(tmp_path.rglob('*.json'))


@pytest.mark.parametrize('name,value',[('ema_source','SubnetFastMovingPrice'),('method','other')])
def test_unknown_method_cannot_reuse_evidence(tmp_path,name,value):
    _,p=proof(tmp_path);p[name]=value
    with pytest.raises(ValueError):validate_reference_proof(p)


@pytest.mark.parametrize('key',['runtime','parent_runtime'])
def test_unreviewed_runtime_is_not_a_valid_rehashed_proof(tmp_path,key):
    from noah.chain.token_context import CODES
    _,p=proof(tmp_path);p[key]=dict(spec_version=486,transaction_version=1,code_hash=CODES[486])
    with pytest.raises(ValueError):validate_reference_proof(p)


def ema_fixture(tmp_path):
    f,p=financial_fixture(tmp_path)
    chain=EmaChain();p.collector=collector(tmp_path,chain)
    p.ema=True;p.ema_document={'effective_block':50,'ema_source':'SubnetMovingPrice'}
    p.ema_collector=EmaReference(chain,tmp_path,2,'SubnetMovingPrice')
    from noah.chain.store import Store
    from noah.points.receipts import from_evidence
    row=f['miners'][0];hot=f['pointsHotkeys']['0']
    store=Store.open(tmp_path/'history.db',day_anchor_ts=0)
    adapter=SimpleNamespace(netuid=2,_foreign_valuation_evidence=[])
    p.precollect_foreign(store.foreign_receipts_for_hotkey(row['coldkey'],hot,2))
    value=store.foreign_deposit_equivalent_for_hotkey(row['coldkey'],hot,2,p.foreign_rule(adapter,None))
    store.close()
    row['days']['2']['foreignDepositNoahEquivRao']=str(value)
    f.update(schema=ema_policy.INPUT_SCHEMA,pointsReceipts=from_evidence(adapter._foreign_valuation_evidence,ema=True),
        pointsPricing=p.ema_collector.documents,pointsPricingBoundary=dict(anchor={'block':49,'hash':h(49)},effectiveBlock=50,
            policySha256='a'*64,predecessorPolicySha256='b'*64,emaSource='SubnetMovingPrice',genesis=chain.genesis_hash(),nativeNetuid=2))
    return f,p


def test_actual_store_duplicate_events_runtime_rule_and_independent_twins(tmp_path):
    f,p=ema_fixture(tmp_path)
    assert validate_fixture(f)
    output=agreed(f)
    assert output['schema']==ema_policy.OUTPUT_SCHEMA and output['policy']==ema_policy.POLICY
    assert len(output['receiptDispositions'])==2
    assert sum(int(r['credit_rao']) for r in output['receiptDispositions'])==10**9
    assert p.collector.chain.calls['ema_snapshot']==1


@pytest.mark.parametrize('mutation',['credit','amount','raw','generation','boundary','chain','native','source'])
def test_both_engines_reject_rehashed_proof_or_financial_tampering(tmp_path,mutation):
    from noah.points.engine import evaluate
    f,_=ema_fixture(tmp_path)
    if mutation=='credit':f['pointsReceipts'][0]['credit_rao']='999'
    elif mutation=='amount':f['pointsReceipts'][0]['amount_rao']='99'
    elif mutation=='boundary':f['pointsPricingBoundary']['anchor']['hash']='0x'+'f'*64
    elif mutation=='chain':f['pointsPricingBoundary']['genesis']='0x'+'f'*64
    elif mutation=='native':f['pointsPricingBoundary']['nativeNetuid']=4
    elif mutation=='source':f['pointsPricingBoundary']['emaSource']='SubnetFastMovingPrice'
    else:
        old,p=next(iter(f['pointsPricing'].items()));p=copy.deepcopy(p)
        if mutation=='raw':p['ema_raw']['asset']=raw(1,16)
        else:p['receipt_asset'][2]=raw(2,8)
        new=digest(p);f['pointsPricing']={new:p}
        for row in f['pointsReceipts']:row['reference_proof']={'path':'points-reference-proofs/'+new+'.json','sha256':new}
    with pytest.raises((ValueError, Unverified)):evaluate(f)
    with pytest.raises(ValueError):typescript_text(f)


def test_mixed_receipt_blocks_keep_old_credits_after_restart(tmp_path):
    from noah.chain.store import Store
    from noah.points.receipts import from_evidence
    f,p=financial_fixture(tmp_path)
    old_rows=copy.deepcopy(f['pointsReceipts'])
    chain=EmaChain();p.collector=collector(tmp_path,chain)
    p.ema=True;p.ema_document={'effective_block':51,'ema_source':'SubnetMovingPrice'}
    p.ema_collector=EmaReference(chain,tmp_path,2,'SubnetMovingPrice')
    row=f['miners'][0];hot=f['pointsHotkeys']['0']
    store=Store.open(tmp_path/'history.db',day_anchor_ts=0)
    try:
        store.record_foreign_deposit(coldkey=row['coldkey'],block_timestamp=51*3600,amount_rao=10**9,
            token_netuid=3,
            block=51,event_ordinal=3,extrinsic_index=1,block_hash=h(51),
            hotkey=old_rows[0]['destination_hotkey'],origin_hotkey=hot,owner_at_deposit=row['coldkey'])
        for _ in range(2):
            adapter=SimpleNamespace(netuid=2,_foreign_valuation_evidence=[])
            p.precollect_foreign(store.foreign_receipts_for_hotkey(row['coldkey'],hot,2))
            total=store.foreign_deposit_equivalent_for_hotkey(row['coldkey'],hot,2,p.foreign_rule(adapter,None))
            rows=from_evidence(adapter._foreign_valuation_evidence,ema=True)
            assert rows[:2]==old_rows
            assert total==3500000000
            assert rows[2]['credit_rao']=='500000000' and rows[2]['reason']=='ema-spot-reference'
            p.ema_collector=EmaReference(chain,tmp_path,2,'SubnetMovingPrice')
    finally:store.close()
    assert chain.calls['ema_snapshot']==2
