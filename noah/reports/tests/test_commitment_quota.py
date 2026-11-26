"""Quota preflight: native dispatch arithmetic and no-send lifecycle boundaries."""
import copy
from types import SimpleNamespace as NS

import pytest

from noah.reports import miner
from noah.reports.backing.chain_session import SubstrateChain
from noah.reports.backing.commitment_quota import (
    U64_MAX, assess_quota, read_commitment_quota, required_space_for_payload,
)
from noah.reports.backing.constants import RUNTIME_483_CODE_HASH
from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.backing.submit import SubmitError
from noah.reports.common import ReportError, state_directory
from noah.reports.tests.test_miner_commands import case, sign, no_sign

PIN='0x'+'ab'*32
GENESIS='0x'+'11'*32
HOT=ss58_encode(b'\x33'*32)


class SDK:
    def __init__(self):
        self.calls=[];self.epoch=50;self.usage={'last_epoch':50,'used_space':3000}
        self.cap=3100;self.timestamp=10000;self.code=RUNTIME_483_CODE_HASH;self.spec=483
        self.bad_canonical=False;self.fail_query=None
    def rpc_request(self,method,params):
        self.calls.append((method,params))
        if method=='chain_getFinalizedHead':return {'result':PIN}
        if method=='chain_getHeader':assert params==[PIN];return {'result':{'number':'0x64'}}
        if method=='chain_getBlockHash':
            assert params in ([100],[0]);return {'result':GENESIS if params==[0] else GENESIS if self.bad_canonical else PIN}
        if method=='state_getRuntimeVersion':assert params==[PIN];return {'result':{'specVersion':self.spec,'transactionVersion':1}}
        if method=='state_getStorageHash':assert params==['0x3a636f6465',PIN];return {'result':self.code}
        raise AssertionError(method)
    def query(self,module,name,params,*,block_hash):
        assert block_hash==PIN
        self.calls.append((module+'.'+name,params,block_hash))
        if name==self.fail_query:raise ConnectionError('synthetic-private-endpoint')
        if name=='UsedSpaceOf':assert params==[82,HOT];return NS(value=copy.deepcopy(self.usage))
        if name=='SubnetEpochIndex':assert params==[82];return NS(value=self.epoch)
        if name=='MaxSpace':assert params==[];return NS(value=self.cap)
        if name=='Now':assert params==[];return NS(value=self.timestamp)
        raise AssertionError(name)


@pytest.mark.parametrize('epoch,last,used,need,cap,allowed',[
    (50,50,3000,100,3100,True),(50,50,3001,100,3100,False),
    (51,50,3100,203,3100,True),(49,50,3100,203,3100,True),
    (0,0,0,100,99,False),(50,50,U64_MAX,203,3100,False),
    (50,50,2191,909,3100,True),(50,50,2192,909,3100,False),
])
def test_exact_native_reset_equality_capacity_and_saturation(epoch,last,used,need,cap,allowed):
    result=assess_quota(epoch_index=epoch,stored_usage={'last_epoch':last,'used_space':used},
                        max_space=cap,required_space=need)
    assert result['allowed'] is allowed
    assert result['effective_used_space']==(used if last==epoch else 0)
    assert result['used_space_after']==min(U64_MAX,result['effective_used_space']+need)


@pytest.mark.parametrize('value',[True,-1,2**64,'50',None,1.0])
def test_counter_requires_native_u64(value):
    with pytest.raises(ReportError):assess_quota(epoch_index=value,stored_usage=None,max_space=3100,required_space=100)


@pytest.mark.parametrize('usage',[{},[],{'last_epoch':0},{'last_epoch':0,'used_space':0,'extra':0},
    {'last_epoch':True,'used_space':0},{'last_epoch':0,'used_space':-1},
    {'last_epoch':0,'used_space':2**64},{'last_epoch':'0','used_space':0}])
def test_invalid_decoded_usage_never_becomes_zero(usage):
    with pytest.raises(ReportError):assess_quota(epoch_index=0,stored_usage=usage,max_space=3100,required_space=100)


def test_absent_usage_is_actual_option_default_and_receipt_pins_all_reads():
    sdk=SDK();sdk.usage=None
    receipt=read_commitment_quota(sdk,mining_netuid=82,hotkey=HOT,required_space=909)
    assert receipt['schema']=='noah.commitment-quota/1' and receipt['allowed'] is True
    assert receipt['finalized_block_hash']==PIN and receipt['finalized_block_number']==100
    assert receipt['stored_usage'] is None and receipt['effective_used_space']==0
    assert receipt['runtime']['code_hash']==RUNTIME_483_CODE_HASH
    assert receipt['runtime']['qualification']=='spec-and-code'
    assert receipt['required_space']==909 and receipt['hotkey']==HOT
    assert not any('nonce' in str(c).lower() or 'author_' in str(c) for c in sdk.calls)


@pytest.mark.parametrize('change',['code','spec','boolspec','canonical','readerror','capbool','capoverflow','usageshape'])
def test_failed_or_unsupported_reads_refuse(change):
    sdk=SDK()
    if change=='code':sdk.code='0x'+'00'*32
    elif change=='spec':sdk.spec=999
    elif change=='boolspec':sdk.spec=True
    elif change=='canonical':sdk.bad_canonical=True
    elif change=='readerror':sdk.fail_query='UsedSpaceOf'
    elif change=='capbool':sdk.cap=True
    elif change=='capoverflow':sdk.cap=2**32
    else:sdk.usage={}
    with pytest.raises(ReportError) as error:read_commitment_quota(sdk,mining_netuid=82,hotkey=HOT,required_space=100)
    assert 'synthetic-private-endpoint' not in str(error.value)


@pytest.mark.parametrize('length,cost',[(32,100),(79,100),(203,203),(207,207)])
def test_exact_single_field_phase_charge(length,cost):
    assert required_space_for_payload(bytes(length))==cost


def adapter_case(case,*,context=True):
    config,contract,fake,engine=case
    if context:contract.update(eligibility_policy_version=REPORT_POLICY,commitment_format='context-v1')
    miner.prepare_report(config,contract,*([] if context else [90,3]),compute_fn=lambda *_:engine)
    chain=SubstrateChain.__new__(SubstrateChain);sdk=SDK();sdk.timestamp=contract['commit_open_ms'];chain.substrate=sdk
    for name in ('genesis','is_registered','commitment','sign','find','finality'):
        setattr(chain,name,getattr(fake,name))
    chain.finalized_time_ms=lambda:sdk.timestamp
    def broadcast(handle):
        fake.now=sdk.timestamp;result=fake.broadcast(handle)
        if len(handle['payload'])==79:fake.stored=handle['payload']
        return result
    chain.broadcast=broadcast
    return config,contract,fake,chain,sdk


def test_wait_then_epoch_reset_progresses_without_consuming_attempt(case):
    c,r,fake,chain,sdk=adapter_case(case);sdk.usage['used_space']=3001
    result=miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert result['action']=='waiting' and result['reason']=='commitment_quota' and not result['sent']
    folder=state_directory(c,r)
    assert not list(folder.glob('*.journal')) and fake.sent==[]
    sdk.epoch+=1
    result=miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)
    assert result['sent'] and result['commitment_quota']['required_space']==100
    assert len(fake.sent)==1 and len(fake.sent[0]['payload'])==79
    sdk.timestamp=r['reveal_open_ms'];sdk.usage={'last_epoch':sdk.epoch,'used_space':3000}
    result=miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert result['reason']=='commitment_quota' and result['commitment_quota']['required_space']==203
    assert not (folder/'reveal.journal').exists() and len(fake.sent)==1
    sdk.usage['used_space']=2897
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
    assert len(fake.sent)==2


def test_unreviewed_quota_rules_allow_chain_decision_without_quoting_capacity(case):
    c, r, fake, chain, sdk = adapter_case(case)
    sdk.spec = 999
    result = miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=sign)
    assert result['sent'] and 'commitment_quota' not in result
    assert len(fake.sent) == 1
    assert not any(call[0] == 'Commitments.UsedSpaceOf' for call in sdk.calls)


def test_chain_dispatch_error_is_retained_for_failed_report(case):
    from dataclasses import replace
    c, r, fake, chain, sdk = adapter_case(case)
    sdk.spec = 999
    dispatch = chain.broadcast
    chain.broadcast = lambda handle: replace(dispatch(handle), success=False,
                                                dispatch_error='CommitmentSpaceExceeded')
    with pytest.raises(SubmitError, match='extrinsic failed: CommitmentSpaceExceeded'):
        miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=sign)
    with pytest.raises(SubmitError, match='previous extrinsic failed: CommitmentSpaceExceeded'):
        miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=no_sign)
    assert len(fake.sent) == 1


def test_legacy_payload_cost_and_after_read_deadline_preserve_original_windows(case):
    c,r,fake,chain,sdk=adapter_case(case,context=False)
    # First clock read is open; the pinned quota observation crosses the deadline.
    chain.finalized_time_ms=lambda:r['commit_open_ms'];sdk.timestamp=r['commit_close_ms']
    result=miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert result['action']=='expired' and not result['sent']
    assert result['commitment_quota']['required_space']==100
    assert not list(state_directory(c,r).glob('*.journal')) and fake.sent==[]


def test_unknown_send_reconciles_without_reopening_quota_or_signer(case):
    c,r,fake,chain,sdk=adapter_case(case);broadcast=chain.broadcast
    def uncertain(handle):broadcast(handle);raise ConnectionError('synthetic lost response')
    chain.broadcast=uncertain
    with pytest.raises(SubmitError,match='unknown'):miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)
    chain.commitment_quota=lambda *_:pytest.fail('existing journal must not preflight a new attempt')
    fake.visible=False
    with pytest.raises(SubmitError,match='unknown'):miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    fake.visible=True
    result=miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    assert not result['sent'] and result['commit']=='finalized' and len(fake.sent)==1


@pytest.mark.parametrize('spec', [495, 497])
def test_current_runtime_requires_the_reviewed_code_and_transaction_two(spec):
    from noah.reports.backing.constants import RUNTIME_CODE_PINS
    code = RUNTIME_CODE_PINS[spec]
    sdk = SDK()
    sdk.spec = spec
    sdk.code = code
    with pytest.raises(ReportError, match='unsupported finalized runtime'):
        read_commitment_quota(sdk, mining_netuid=82, hotkey=HOT, required_space=100)
    original = sdk.rpc_request
    def version_two(method, params):
        response = original(method, params)
        if method == 'state_getRuntimeVersion':
            response = {'result': {'specVersion': spec, 'transactionVersion': 2}}
        return response
    sdk.rpc_request = version_two
    receipt = read_commitment_quota(sdk, mining_netuid=82, hotkey=HOT, required_space=100)
    assert receipt['runtime']['qualification'] == 'spec-and-code'
    assert receipt['runtime']['spec_version'] == spec
    assert receipt['runtime']['code_hash'] == code
    sdk.code = '0x' + '00' * 32
    with pytest.raises(ReportError, match='unsupported finalized runtime'):
        read_commitment_quota(sdk, mining_netuid=82, hotkey=HOT, required_space=100)


def test_490_receipt_records_observed_code_without_claiming_code_allowlist():
    sdk=SDK();sdk.spec=490;sdk.code='0x'+'cd'*32
    receipt=read_commitment_quota(sdk,mining_netuid=82,hotkey=HOT,required_space=100)
    assert receipt['runtime']['qualification']=='supported-spec'
    assert receipt['runtime']['code_hash']==sdk.code


@pytest.mark.parametrize('condition',['readerror','genesis'])
def test_quota_failure_never_loads_wallet_or_creates_journal(case,condition):
    c,r,fake,chain,sdk=adapter_case(case)
    if condition=='readerror':sdk.fail_query='UsedSpaceOf'
    else:
        rpc=sdk.rpc_request
        sdk.rpc_request=lambda method,params: {'result':'0x'+'ff'*32} if method=='chain_getBlockHash' and params==[0] else rpc(method,params)
    with pytest.raises(ReportError):miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert fake.sent==[] and not list(state_directory(c,r).glob('*.journal'))


def test_actual_retained_metadata_decodes_usage_fields_and_counter_without_network():
    from noah.reports.tests.raw_export_fixture import RawChain, HOT as RAW_HOT
    raw=RawChain();sdk=raw.sdk;hotkey=ss58_encode(RAW_HOT)
    for module,name,params,value in [
        ('SubtensorModule','SubnetEpochIndex',[82],50),
        ('Commitments','UsedSpaceOf',[82,hotkey],{'last_epoch':50,'used_space':2191}),
        ('Commitments','MaxSpace',[],3100),
    ]:
        key=sdk.create_storage_key(module,name,params)
        obj=sdk.runtime_config.create_scale_object(key.value_scale_type,metadata=sdk.metadata)
        raw.storage[key.to_hex()]=str(obj.encode(value))
    original=raw.call
    def call(method,params):
        if method=='state_getStorageHash':
            from noah.reports.backing.constants import RUNTIME_492_CODE_HASH
            return RUNTIME_492_CODE_HASH  # Raw fixture now advertises reviewed492.
        return original(method,params)
    raw.call=call
    receipt=read_commitment_quota(sdk,mining_netuid=82,hotkey=hotkey,required_space=909)
    assert receipt['stored_usage']=={'last_epoch':50,'used_space':2191}
    assert receipt['epoch_index']==50 and receipt['max_space']==3100
    assert receipt['allowed'] and receipt['used_space_after']==3100


@pytest.mark.parametrize('phase',['commit','reveal'])
def test_slow_quota_queries_expire_before_wallet_and_keep_pinned_receipt(case,phase):
    c,r,fake,chain,sdk=adapter_case(case);sdk.usage=None
    if phase=='reveal':
        assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
        sdk.timestamp=r['reveal_open_ms']
    prior_sends=len(fake.sent)
    current_time=[r[phase+'_open_ms']]
    chain.finalized_time_ms=lambda:current_time[0]
    original=chain.commitment_quota;receipts=[]
    def delayed(*args):
        receipt=original(*args);receipts.append(copy.deepcopy(receipt))
        # No alteration of the pinned timestamp: real time advances during reads.
        current_time[0]=r[phase+'_close_ms']
        return receipt
    chain.commitment_quota=delayed
    result=miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)
    assert result['action']=='expired' and not result['sent']
    assert result['commitment_quota']==receipts[0]
    assert result['commitment_quota']['finalized_timestamp_ms']==r[phase+'_open_ms']
    assert not (state_directory(c,r)/(phase+'.journal')).exists()
    assert len(fake.sent)==prior_sends


@pytest.mark.parametrize('phase',['commit','reveal'])
def test_slow_wallet_unlock_expires_before_phase_journal_or_signing(case,phase):
    c,r,fake,chain,sdk=adapter_case(case);sdk.usage=None
    if phase=='reveal':
        assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
        sdk.timestamp=r['reveal_open_ms']
    prior_sends=len(fake.sent);wallet_reads=[]
    def delayed_wallet(config):
        wallet_reads.append(True)
        sdk.timestamp=r[phase+'_close_ms']
        return sign(config) # Synthetic key interface; no signing is invoked.
    def forbidden_sign(*_args,**_kwargs):pytest.fail('phase closed during wallet unlock')
    chain.sign=forbidden_sign
    with pytest.raises(SubmitError,match='outside the announced window'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=delayed_wallet)
    assert wallet_reads==[True]
    assert not (state_directory(c,r)/(phase+'.journal')).exists()
    assert len(fake.sent)==prior_sends
