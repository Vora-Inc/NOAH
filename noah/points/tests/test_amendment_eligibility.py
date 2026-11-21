import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from noah.chain.sources import SourceDisagreement, Unverified
from noah.chain.token_context import CODES
from noah.points.collector import Collector
from noah.points.eligibility import ReferenceEligibility, validate_reference_proof, verify_dispositions
from noah.points import amendment
from noah.points.engine import evaluate
from noah.points.run import agreed
from noah.points.tests.test_integration import captured
from noah.core.calculation_record import encoded

R = 10**9


def h(block):
    return '0x' + f'{block + 1000:064x}'


def raw(value, width):
    return '0x' + value.to_bytes(width, 'little').hex()


class Chain:
    def __init__(self):
        self.calls = Counter()
        self.change = None
        self.zero = None
        self.failure = None
    def genesis_hash(self): return h(0)
    def block_hash(self, block): return h(block)
    def head_block(self): return 100
    def block_timestamp(self, block): return block * 3600
    def token_reference_context(self, block, assets):
        self.calls['life'] += 1
        self.calls[('life', block)] += 1
        if self.failure and self.failure[0] == block:
            raise self.failure[1]
        rows = {}
        for asset in assets:
            values = dict(NetworksAdded=(True,1), NetworkRegisteredAt=(1,8),
                          RegisteredSubnetCounter=(1,8), SubnetMechanism=(1,2))
            if self.change:
                self.change(block, asset, values)
            rows[str(asset)] = {name: {'raw': None if value is None else raw(value,width),
                                     'value': value, 'default':'0x'+'00'*width, 'key':'unused'}
                                for name,(value,width) in values.items()}
        runtime = dict(spec_version=483,code_hash=CODES[483],transaction_version=1)
        return dict(block=block,block_hash=h(block),parent_hash=h(block-1),runtime=runtime,
                    parent_runtime=runtime,metadata_sha256='a'*64,assets=rows)
    def reference_price_at(self, block, netuid, *, expected_block_hash):
        assert expected_block_hash == h(block)
        self.calls['price'] += 1
        if self.zero == (block,netuid): return 0
        return 2*R if netuid==2 else 3*R


def collector(tmp_path, chain=None):
    chain = chain or Chain()
    c = Collector(chain,tmp_path/'points-prices',2)
    c.eligibility = ReferenceEligibility(chain,2)
    return c


def test_supported_full_window_group_and_many_receipts_share_one_walk(tmp_path):
    c = collector(tmp_path)
    c.precollect(50,[0,3,4])
    for block in range(50,70):
        for asset in (0,3,4):
            assert c.eligibility.result(c,block,asset)['status']=='supported'
    assert all(c.chain.calls[('life',block)]==1 for block in range(24,48))
    assert c.chain.calls['price']==72
    files=list((tmp_path/'points-reference-proofs').glob('*.json'))
    docs=[json.loads(p.read_bytes()) for p in files]
    assert sum(d['schema']=='noah-token-reference-proof/1' for d in docs)==3
    assert sum(d['schema']=='noah-token-reference-receipt/1' for d in docs)==60
    assert len(c.prior_day(50,3))==24


def test_restart_cache_rechecks_lifecycle_once_and_no_price_reads(tmp_path):
    c=collector(tmp_path);c.precollect(50,[3])
    newer=collector(tmp_path);newer.precollect(51,[3])
    for block in range(52,60):newer.eligibility.result(newer,block,3)
    assert newer.chain.calls['price']==0
    assert all(newer.chain.calls[('life',block)]==1 for block in range(24,48))


@pytest.mark.parametrize('change,reason',[
    (lambda b,a,v: v.update(NetworksAdded=(None,1)) if b==30 and a==3 else None,'reference-subnet-absent'),
    (lambda b,a,v: v.update(RegisteredSubnetCounter=(0,8)) if b==30 and a==3 else None,'reference-token-incarnation-differs'),
    (lambda b,a,v: v.update(NetworkRegisteredAt=(0,8)) if b==30 and a==2 else None,'reference-token-incarnation-differs'),
])
def test_unsupported_midwindow_not_masked_by_positive_prices(tmp_path,change,reason):
    c=collector(tmp_path);c.chain.change=change
    c.precollect(50,[3,4])
    result=c.eligibility.result(c,50,3)
    assert result['status']=='unsupported' and result['reason']==reason
    sibling=c.eligibility.result(c,50,4)
    assert sibling['status'] in ('supported','unsupported')
    if reason=='reference-subnet-absent':assert sibling['status']=='supported'


@pytest.mark.parametrize('asset,reason',[(3,'foreign-reference-price-zero'),(2,'native-reference-price-zero')])
def test_paired_successful_zero_is_terminal(tmp_path,asset,reason):
    c=collector(tmp_path);c.chain.zero=(30,asset)
    c.precollect(50,[3])
    result=c.eligibility.result(c,50,3)
    assert result['status']=='unsupported' and result['reason']==reason


def test_default_zero_birth_marker_is_not_blanket_rejected(tmp_path):
    c=collector(tmp_path)
    def change(b,a,v):
        if a==1:v.update(NetworkRegisteredAt=(None,8),RegisteredSubnetCounter=(None,8),SubnetMechanism=(None,2))
    c.chain.change=change
    c.precollect(50,[1])
    assert c.eligibility.result(c,50,1)['status']=='supported'


def test_receipt_block_generation_change_is_pending(tmp_path):
    c=collector(tmp_path)
    c.chain.change=lambda b,a,v:v.update(RegisteredSubnetCounter=(2,8)) if b==50 and a==3 else None
    with pytest.raises(Unverified):c.precollect(50,[3])
    assert not (tmp_path/'points-reference-proofs').exists()


def test_uncertainty_and_disagreement_are_never_terminal(tmp_path):
    c=collector(tmp_path);c.chain.failure=(30,SourceDisagreement('changed reader'))
    with pytest.raises(SourceDisagreement):c.precollect(50,[3])
    assert not (tmp_path/'points-reference-proofs').exists()


def test_warm_price_cache_cannot_bypass_new_lifecycle_failure(tmp_path):
    c=collector(tmp_path);c.precollect(50,[3])
    restarted=collector(tmp_path)
    restarted.chain.change=lambda b,a,v:v.update(NetworksAdded=(None,1)) if b==30 and a==3 else None
    restarted.precollect(50,[3])
    assert restarted.eligibility.result(restarted,50,3)['status']=='unsupported'


def test_shared_proof_missing_or_changed_refuses(tmp_path):
    c=collector(tmp_path);c.precollect(50,[3])
    ref=c.eligibility.result(c,50,3)['proof']
    receipt=json.loads((tmp_path/ref['path']).read_bytes())
    loader=lambda ref:(tmp_path/ref['path']).read_bytes()
    assert validate_reference_proof(receipt,read_proof=loader)==ref['sha256']
    (tmp_path/receipt['window_proof']['path']).write_text('{}')
    with pytest.raises(ValueError):validate_reference_proof(receipt,read_proof=loader)


def test_envelope_preserves_exact_predecessor_and_requires_new_identity(tmp_path):
    _,old=captured(tmp_path)
    raw_bytes=(json.dumps(old,indent=3)+'\n').encode()
    document=amendment.build(raw_bytes)
    assert document['predecessor_json'].encode()==raw_bytes
    assert amendment.validate_document(document)==old
    assert document['effective_day']==old['migration']['cutoff_day']+1
    for field,value in [('effective_day',99),('rule','changed'),('predecessor_sha256','0'*64)]:
        altered={**document,field:value}
        with pytest.raises(ValueError):amendment.validate_document(altered)


def test_amended_twin_keeps_financial_values_changes_only_identity(tmp_path):
    _,old=captured(tmp_path)
    fixture=old['fixture']
    legacy=evaluate(fixture)
    amended={**fixture,'schema':amendment.INPUT_SCHEMA,'pointsReceipts':[],'pointsHotkeys':{str(r['uid']):r['hotkey'] for r in old['migration']['miners']},'pointsOwners':{str(r['uid']):r['coldkey'] for r in old['migration']['miners']}}
    value=agreed(amended)
    assert value['schema']=='noah-points-output/2' and value['policy']==amendment.POLICY
    assert value['receiptDispositions']==[]
    assert value['miners']==legacy['miners'] and value['weightsU16']==legacy['weightsU16']


def runtime_policy(c):
    from noah.points.runtime import RuntimePolicy
    p=object.__new__(RuntimePolicy)
    p.collector=c;p.amended=True;p.migration={'cutoff_block':23,'cutoff_day':0,'genesis':h(0)}
    p.manifest={'sha256':'a'*64,'netuid':2};p.old={};p.day_anchor_ts=0
    return p


def test_runtime_precollection_uses_each_assets_actual_receipt_block(tmp_path):
    c=collector(tmp_path)
    def change(b,a,v):
        if a==4:
            if b<55:v.update(NetworksAdded=(None,1),NetworkRegisteredAt=(None,8),RegisteredSubnetCounter=(None,8))
            else:v.update(NetworkRegisteredAt=(55,8))
    c.chain.change=change
    p=runtime_policy(c)
    p.precollect_foreign([(R,3,50,h(50)),(R,4,60,h(60))])
    assert c.eligibility.result(c,50,3)['status']=='supported'
    assert c.eligibility.result(c,60,4)['status']=='unsupported'
    assert c.chain.calls[('life',24)]==1


def financial_fixture(tmp_path, *, unsupported=False):
    from noah.chain.store import Store
    from noah.chain.ss58 import address_of
    from noah.points.tests.test_engine import fixture
    from noah.points.receipts import from_evidence
    c=collector(tmp_path)
    if unsupported:c.chain.zero=(30,3)
    p=runtime_policy(c)
    cold,hot,dest=[address_of(bytes([i])*32) for i in (1,2,3)]
    adapter=SimpleNamespace(netuid=2,_foreign_valuation_evidence=[])
    store=Store.create(tmp_path/'history.db',day_anchor_ts=0)
    store.mark_day_scanned(2,48,71)
    for ordinal in (1,2):
        for _ in range(2):
            store.record_foreign_deposit(coldkey=cold,block_timestamp=50*3600,amount_rao=R,
                token_netuid=3,
                block=50, event_ordinal=ordinal, extrinsic_index=1, block_hash=h(50),
                hotkey=dest,origin_hotkey=hot,owner_at_deposit=cold)
    p.precollect_foreign(store.foreign_receipts_for_hotkey(cold,hot,2))
    amount=store.foreign_deposit_equivalent_for_hotkey(cold,hot,2,p.foreign_rule(adapter,None))
    store.close()
    f=fixture([[(R,0,0),(0,0,0),(0,0,amount)]])
    f['miners'][0]['coldkey']=cold
    f.update(schema=amendment.INPUT_SCHEMA,pointsHotkeys={'0':hot},pointsOwners={'0':cold},pointsReceipts=from_evidence(adapter._foreign_valuation_evidence),
             pointsMigration={'policySha256':'a'*64,'cutoffDay':0,'balances':[{'uid':0,'coldkey':cold,'balanceRao':'0'}]})
    return f,p


@pytest.mark.parametrize('unsupported',[False,True])
def test_store_exact_event_dedup_to_twins_and_retained_proof(tmp_path,unsupported):
    f,p=financial_fixture(tmp_path,unsupported=unsupported)
    result=agreed(f)
    assert len(result['receiptDispositions'])==2
    assert len({r['receipt_id'] for r in result['receiptDispositions']})==2
    assert sum(int(r['credit_rao']) for r in f['pointsReceipts'])==(0 if unsupported else 3*R)
    assert verify_dispositions(tmp_path,f['pointsReceipts'],p)
    assert result['miners'][0]['state']['balance']==str(-R//2 if unsupported else 5*R//2)


@pytest.mark.parametrize('tamper',['foreign','owner','day','hotkey','drop','duplicate'])
def test_valid_proofs_cannot_hide_changed_financial_inputs(tmp_path,tamper):
    from noah.points.run import typescript_text
    f,p=financial_fixture(tmp_path,unsupported=True)
    if tamper=='foreign':f['miners'][0]['days']['2']['foreignDepositNoahEquivRao']='1'
    elif tamper=='owner':f['pointsReceipts'][0]['coldkey']=f['pointsReceipts'][0]['destination_hotkey']
    elif tamper=='day':f['pointsReceipts'][0]['day']=3
    elif tamper=='hotkey':f['pointsHotkeys']['0']=f['pointsReceipts'][0]['destination_hotkey']
    elif tamper=='drop':
        # Omitted zero-credit rows cannot change the aggregate but are separately
        # rejected against complete retained valuation context by publication.
        f['pointsReceipts'][0]['credit_rao']='1'
    else:f['pointsReceipts'].append(copy.deepcopy(f['pointsReceipts'][0]))
    with pytest.raises(ValueError):evaluate(f)
    with pytest.raises(ValueError):typescript_text(f)


def test_retained_day_must_match_proven_timestamp(tmp_path):
    f,p=financial_fixture(tmp_path)
    f['pointsReceipts'][0]['day']=1
    with pytest.raises(ValueError):verify_dispositions(tmp_path,f['pointsReceipts'],p)


def test_normal_v3_startup_requires_explicit_install_and_archive(tmp_path):
    from noah.points.runtime import RuntimePolicy
    adapter,old=captured(tmp_path)
    value=amendment.build(encoded(old))
    with pytest.raises(ValueError,match='explicitly installed'):
        RuntimePolicy(value,adapter)
    (tmp_path/'points-policy.json').write_bytes(encoded(value))
    with pytest.raises(FileNotFoundError):RuntimePolicy(value,adapter)
    (tmp_path/'points-policy-predecessor.json').write_bytes(encoded(old))
    policy=RuntimePolicy(value,adapter)
    from noah.points.lineages import validate_document
    assert policy.baselines==validate_document(old,netuid=adapter.netuid)
    assert policy.migration==old['migration']
    assert policy.manifest==amendment.manifest(value,adapter.netuid)
    assert policy.input_schema==amendment.INPUT_SCHEMA


def test_full_authenticated_roster_can_contain_legitimately_excluded_seats(tmp_path):
    from noah.chain.ss58 import address_of
    f,_=financial_fixture(tmp_path)
    f['pointsHotkeys']['99']=address_of(bytes([9])*32)
    f['pointsOwners']['99']=f['miners'][0]['coldkey']
    assert agreed(f)['receiptDispositions']==f['pointsReceipts']
    f['pointsReceipts'][0]['origin_hotkey']=f['pointsHotkeys']['99']
    with pytest.raises(ValueError):evaluate(f)


def test_future_birth_state_is_unsafe_not_supported_or_terminal(tmp_path):
    from noah.chain.sources import SourceLied
    c=collector(tmp_path)
    c.chain.change=lambda b,a,v:v.update(NetworkRegisteredAt=(100,8)) if b==30 and a==3 else None
    with pytest.raises(SourceLied):c.precollect(50,[3])


def test_corrupt_warm_cache_is_unsafe_not_availability(tmp_path):
    from noah.chain.sources import SourceLied
    c=collector(tmp_path);c.precollect(50,[3])
    next((tmp_path/'points-prices').glob('*.json')).write_text('{}')
    fresh=collector(tmp_path)
    with pytest.raises(SourceLied):fresh.precollect(50,[3])


def test_proof_arithmetic_uses_exact_authenticated_bytes_once(tmp_path):
    f,p=financial_fixture(tmp_path)
    reads=Counter()
    def changing(reference):
        reads[reference['path']]+=1
        data=(tmp_path/reference['path']).read_bytes()
        if reads[reference['path']]>1:return b'{}'
        return data
    assert verify_dispositions(None,f['pointsReceipts'],p,read_proof=changing)
    assert max(reads.values())==1


def test_moved_hotkey_keeps_old_wallet_credit_on_its_null_row(tmp_path):
    from noah.chain.ss58 import address_of
    from noah.chain.store import Store
    f,p=financial_fixture(tmp_path)
    original=f['miners'][0]
    oldcold=original['coldkey'];hot=f['pointsHotkeys']['0']
    newcold=address_of(bytes([8])*32)
    # Actual persistent receipt selector remains keyed by both wallet+hotkey.
    store=Store.open(tmp_path/'history.db',day_anchor_ts=0)
    assert len(store.foreign_receipts_for_hotkey(oldcold,hot,2))==2
    assert store.foreign_receipts_for_hotkey(newcold,hot,2)==[]
    store.close()
    newrow=copy.deepcopy(original);newrow['coldkey']=newcold
    for day in newrow['days'].values():day['foreignDepositNoahEquivRao']='0'
    original['uid']=None
    f['miners']=[newrow,original]
    f['pointsOwners']['0']=newcold
    f['pointsMigration']['balances']=[{'uid':0,'coldkey':newcold,'balanceRao':'0'}]
    result=agreed(f)
    assert result['receiptDispositions']==f['pointsReceipts']
    assert sum(int(r['credit_rao']) for r in result['receiptDispositions'])==3*R
    # Reassigning the same exact receipt to the new owner is not selector parity.
    f['pointsReceipts'][0]['coldkey']=newcold
    with pytest.raises(ValueError):evaluate(f)


def test_realistic_7200_interval_proofs_are_bounded_and_share_native_prices(tmp_path):
    c=collector(tmp_path)
    c.chain.block_timestamp=lambda b:b*12
    c.chain.head_block=lambda:20000
    c.precollect(14401,[0,3])
    docs=[json.loads(p.read_bytes()) for p in (tmp_path/'points-reference-proofs').glob('*.json')]
    windows=[d for d in docs if d['schema']=='noah-token-reference-proof/1']
    assert len(windows)==2 and all(len(d['rows'])==7200 for d in windows)
    assert max(len(encoded(d)) for d in windows)<4*1024**2
    assert c.chain.calls['price']==14400 # native reused, root numerator fixed
    for receipt_block in range(14402,14412):
        for asset in (0,3):
            assert c.eligibility.result(c,receipt_block,asset)['status']=='supported'
    assert all(c.chain.calls[('life',block)]==1 for block in range(7200,14400))
    assert c.chain.calls['price']==14400
    fresh=collector(tmp_path)
    fresh.chain.block_timestamp=lambda b:b*12
    fresh.chain.head_block=lambda:20000
    fresh.precollect(14401,[0,3])
    for receipt_block in range(14402,14412):
        for asset in (0,3):fresh.eligibility.result(fresh,receipt_block,asset)
    assert all(fresh.chain.calls[('life',block)]==1 for block in range(7200,14400))
    assert fresh.chain.calls['price']==0
    for asset in (0,3):
        from noah.points.pricing import daily_average_credit
        assert daily_average_credit(R,asset,14401*12,c.prior_day(14401,asset),native_netuid=2)==(R//2 if asset==0 else 3*R//2)


def test_terminal_receipt_cannot_mask_other_receipts_unsafe_failure(tmp_path,monkeypatch):
    from noah.chain.live import ChainUnavailable
    from noah.chain.foreign_value import ForeignDepositUnpriced
    from noah.points.outage import record_failure,authorize
    from noah.points.tests.test_outage import setup
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.starter.runtime import republish
    authority,transport,_,source,now=setup(tmp_path/'authority',monkeypatch)
    c=collector(tmp_path/'pending');p=runtime_policy(c)
    c.chain.failure=(31,ChainUnavailable('synthetic temporarily unavailable'))
    with pytest.raises(ForeignDepositUnpriced) as missing:p.precollect_foreign([(R,3,50,h(50)),(R,4,50,h(50))])
    assert record_failure(authority,missing.value)
    assert authorize(authority,source,now[0]) is not None
    c.chain.zero=(30,3)
    c.chain.failure=(31,SourceDisagreement('synthetic paired contradiction'))
    with pytest.raises(SourceDisagreement) as unsafe:p.precollect_foreign([(R,3,50,h(50)),(R,4,50,h(50))])
    assert record_failure(authority,unsafe.value) is False
    c.chain.failure=(31,ChainUnavailable('synthetic later partial availability failure'))
    with pytest.raises(ForeignDepositUnpriced) as later:p.precollect_foreign([(R,4,50,h(50))])
    assert record_failure(authority,later.value) is False
    sent=len(transport.calls)
    with pytest.raises(PublicationStatusUnknown):republish(authority,560,560)
    assert len(transport.calls)==sent
    assert not list((tmp_path/'pending/points-reference-proofs').glob('*.json'))
