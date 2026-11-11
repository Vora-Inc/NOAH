from types import SimpleNamespace as NS

import pytest
import xxhash

from noah.chain.agreeing import AgreeingChain
from noah.chain.live import LiveRunnerChain, AlphaPriceUnreadable
from noah.chain.sources import SourceDisagreement, SourceLied, Unverified
from noah.chain.token_context import CODES, FIELDS


def h(block):return '0x'+f'{block+1000:064x}'
def height(value):return int(value,16)-1000


def twox(value):return b''.join(xxhash.xxh64(value,seed=i).intdigest().to_bytes(8,'little') for i in (0,1))


class Key:
    def __init__(self,name,asset,kind):
        self.name,self.asset,self.kind=name,asset,kind
        self.value_scale_type=kind
    def to_hex(self):return '0x'+(twox(b'SubtensorModule')+twox(self.name.encode())+self.asset.to_bytes(2,'little')).hex()


class SDK:
    def __init__(self):
        self.price=3
        self.bad_key=False
        self.bad_decode=False
        self.upgrade=False
        self.unknown=False
        self.batch_calls=0
        self.keys={}
        self.override={}
    def get_block_hash(self,block):return h(block)
    def code(self,b):return 488 if self.upgrade and b>=50 else 486
    def md(self,spec):return b'meta\x0f'+bytes([spec%256])*5
    def rpc_request(self,method,args):
        if method=='chain_getHeader':
            b=height(args[0]);value={'number':hex(b),'parentHash':h(b-1)}
        elif method=='state_getRuntimeVersion':value={'specVersion':999 if self.unknown else self.code(height(args[0])),'transactionVersion':1}
        elif method=='state_getStorageHash':value=CODES[self.code(height(args[1]))]
        elif method=='state_call':
            b=self.md(self.code(height(args[-1])));value='0x'+(b'\x01'+bytes([len(b)*4])+b).hex()
        elif method=='state_queryStorageAt':
            self.batch_calls+=1
            value=[{'block':args[1],'changes':[[k,self.raw(self.keys[k])] for k in args[0]]}]
        else:raise AssertionError(method)
        return {'result':value}
    def init_runtime(self,*,block_hash):
        def item(name):
            kind=dict(FIELDS)[name];width={'bool':1,'u16':2,'u64':8}[kind]
            return NS(value={'modifier':'Default'},get_params_type_string=lambda:['u16'],value_object={'default':NS(value_object=b'\x00'*width)})
        data=self.md(self.code(height(block_hash)-1))
        return NS(metadata=NS(data=NS(to_hex=lambda:'0x'+data.hex()),get_metadata_pallet=lambda n:NS(get_storage_function=item)))
    def create_storage_keys(self,pallet,name,params,*,block_hash):
        result=[]
        for [asset] in params:
            key=Key(name,asset,dict(FIELDS)[name]);self.keys[key.to_hex()]=key;result.append(key)
        if self.bad_key:result[0].to_hex=lambda:'0x'+'00'*34
        return result
    def raw(self,key):
        value=self.override.get((key.name,key.asset),True if key.kind=='bool' else 1)
        if value is None:return None
        return '0x'+int(value).to_bytes({'bool':1,'u16':2,'u64':8}[key.kind],'little').hex()
    def query_multi(self,keys,*,block_hash):
        rows=[]
        for key in keys:
            value=self.raw(key)
            value=int.from_bytes(bytes.fromhex(value[2:]),'little') if value else 0
            if key.kind=='bool':value=bool(value)
            if self.bad_decode and key.name=='NetworkRegisteredAt':value=99
            rows.append((key,value))
        return rows
    def runtime_call(self,*args,**kwargs):return self.price


def chain(a=None,b=None):
    a,b=a or SDK(),b or SDK()
    endpoints={'primary':'ws://primary.invalid:9944','secondary':'ws://secondary.invalid:9944'}
    return AgreeingChain({name:LiveRunnerChain(sdk,netuid=2,endpoint=endpoints[name])
                          for name,sdk in [('primary',a),('secondary',b)]},endpoints)


def test_actual_paired_reader_authenticates_raw_default_and_grouped_values():
    a,b=SDK(),SDK()
    a.override[('NetworksAdded',3)]=b.override[('NetworksAdded',3)]=None
    out=chain(a,b).token_reference_context(50,[2,3])
    assert out['assets']['3']['NetworksAdded']['raw'] is None
    assert out['assets']['3']['NetworksAdded']['value'] is False
    assert out['assets']['2']['NetworksAdded']['raw']=='0x01'
    assert a.batch_calls==b.batch_calls==2


def test_reviewed_upgrade_boundary_keeps_target_parent_metadata_distinct():
    a,b=SDK(),SDK();a.upgrade=b.upgrade=True
    out=chain(a,b).token_reference_context(50,[2,3])
    assert out['runtime']['spec_version']==488 and out['parent_runtime']['spec_version']==486


@pytest.mark.parametrize('fault',['bad_key','bad_decode','unknown'])
def test_malformed_or_unreviewed_context_is_never_absence(fault):
    a,b=SDK(),SDK();setattr(a,fault,True)
    with pytest.raises((SourceLied,Unverified)):chain(a,b).token_reference_context(50,[2,3])


def test_actual_paired_disagreement_refuses():
    a,b=SDK(),SDK();b.override[('RegisteredSubnetCounter',3)]=2
    with pytest.raises(SourceDisagreement):chain(a,b).token_reference_context(50,[2,3])


def test_successful_paired_zero_is_distinct_from_none_or_disagreement():
    a,b=SDK(),SDK();a.price=b.price=0
    assert chain(a,b).reference_price_at(50,3,expected_block_hash=h(50))==0
    a.price=None
    with pytest.raises(AlphaPriceUnreadable):chain(a,b).reference_price_at(50,3,expected_block_hash=h(50))
    a.price=1
    with pytest.raises(SourceDisagreement):chain(a,b).reference_price_at(50,3,expected_block_hash=h(50))


def test_returned_rpc_error_is_pending_not_contradiction():
    from noah.chain.live import ChainUnavailable
    a=SDK();original=a.rpc_request
    a.rpc_request=lambda method,args: {'error':{'code':-32000,'message':'unavailable'}} if method=='state_queryStorageAt' else original(method,args)
    with pytest.raises(ChainUnavailable):chain(a,SDK()).token_reference_context(50,[2,3])
