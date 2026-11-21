"""Real Live/Agreeing API tests with exact SCALE/raw-storage boundaries."""
from types import SimpleNamespace as NS
import pytest
from noah.chain.ema_context import fixed
from noah.chain.sources import SourceLied,SourceDisagreement,Unverified
from noah.chain.live import ChainUnavailable
from noah.points.tests.test_token_context import SDK,Key,chain,h,height

FIELD='SubnetMovingPrice'
class EmaSDK(SDK):
    def __init__(self):
        super().__init__();self.ema={2:36045255,3:11488868};self.spot={2:25730638,3:14906966};self.ema_bad_decode=False;self.ema_bad_key=False; self.raw_bad=False;self.disappear=False;self.raw_calls=0;self.version=483;self.absent_field=False;self.price_queries=[]
    def code(self,b):return self.version
    def init_runtime(self,*,block_hash):
        runtime=super().init_runtime(block_hash=block_hash);original=runtime.metadata.get_metadata_pallet('SubtensorModule').get_storage_function
        def item(name):
            if name==FIELD:
                if self.absent_field:return None
                return NS(value={'modifier':'Default'},get_params_type_string=lambda:['u16'],value_object={'default':NS(value_object=b'\x00'*16)})
            return original(name)
        runtime.metadata.get_metadata_pallet=lambda n:NS(get_storage_function=item);return runtime
    def create_storage_keys(self,pallet,name,params,*,block_hash):
        if name!=FIELD:return super().create_storage_keys(pallet,name,params,block_hash=block_hash)
        keys=[]
        for [asset] in params:
            k=Key(name,asset,'I96F32');self.keys[k.to_hex()]=k;keys.append(k)
        if self.ema_bad_key:keys[0].to_hex=lambda:'0x'+'00'*34
        return keys
    def raw(self,key):
        if key.name!=FIELD:return super().raw(key)
        n=self.ema[key.asset]
        if self.raw_bad:return '0x01'
        return None if n is None else '0x'+n.to_bytes(16,'little',signed=True).hex()
    def query_multi(self,keys,*,block_hash):
        if keys[0].name!=FIELD:return super().query_multi(keys,block_hash=block_hash)
        return [(k,{'bits':(self.ema[k.asset] or 0)+(1 if self.ema_bad_decode else 0)}) for k in keys]
    def rpc_request(self,method,args):
        if method=='state_call' and args[0]=='SwapRuntimeApi_current_alpha_price':
            a=int.from_bytes(bytes.fromhex(args[1][2:]),'little');self.price_queries.append(a)
            return {'result':'0x'+self.spot[a].to_bytes(8,'little').hex()}
        if method=='state_queryStorageAt' and self.keys[args[0][0]].name==FIELD:
            self.raw_calls+=1
            if self.disappear and self.raw_calls==2:self.ema[2]=None
        return super().rpc_request(method,args)
    def runtime_call(self,api,name,params,*,block_hash):return self.spot[params[0][0]]

def snapshot(a=None,b=None,**kwargs):
    return chain(a or EmaSDK(),b or EmaSDK()).ema_price_snapshot(50,[2,3],expected_block_hash=h(50),ema_source=kwargs.get('ema_source',FIELD))

def test_actual_paired_api_preserves_exact_raw_and_ratio_units():
    a,b=EmaSDK(),EmaSDK();out=snapshot(a,b)
    assert out['block_hash']==h(50) and out['ema_source']==FIELD
    assert out['prices']['2']['ema']['numerator']==36045255
    assert out['prices']['2']['ema']['denominator']==2**32
    assert out['prices']['3']['spot']=={'raw':'0x5676e30000000000','rao':14906966,'numerator':14906966,'denominator':10**9}
    assert a.price_queries==b.price_queries==[2,3] # never root

@pytest.mark.parametrize('number',[None,0,-1,2**127-1])
def test_absent_stored_zero_and_invalid_signed_price_are_distinct_facts(number):
    a,b=EmaSDK(),EmaSDK();a.ema[3]=b.ema[3]=number
    row=snapshot(a,b)['prices']['3']['ema'];assert row['bits']==number
    assert (row['raw'] is None)==(number is None)

@pytest.mark.parametrize('field',['SubnetFastMovingPrice','BondMovingAverage','',None])
def test_no_implicit_field_switch_or_fast_fallback(field):
    a=EmaSDK()
    with pytest.raises(Unverified):snapshot(a,ema_source=field)
    assert a.price_queries==[]

@pytest.mark.parametrize('fault',['ema_bad_key','ema_bad_decode','raw_bad','disappear'])
def test_raw_key_decode_width_or_mutation_contradiction_refuses(fault):
    a=EmaSDK();setattr(a,fault,True)
    with pytest.raises(SourceLied):snapshot(a)

@pytest.mark.parametrize('kind',['ema','spot','lifecycle'])
def test_every_selected_field_is_paired(kind):
    a,b=EmaSDK(),EmaSDK()
    if kind=='ema':b.ema[3]+=1
    elif kind=='spot':b.spot[3]+=1
    else:b.override[('RegisteredSubnetCounter',3)]=2
    with pytest.raises(SourceDisagreement):snapshot(a,b)

def test_missing_runtime_field_is_unverified_not_default():
    a=EmaSDK();a.absent_field=True
    with pytest.raises(Unverified):snapshot(a)

def test_reviewed_lifecycle_runtime_without_ema_review_refuses():
    a,b=EmaSDK(),EmaSDK();a.version=b.version=486
    with pytest.raises(Unverified):snapshot(a,b)

def test_source_rpc_error_is_pending_not_zero():
    a=EmaSDK();original=a.rpc_request
    def rpc(method,args):
        if method=='state_queryStorageAt' and a.keys[args[0][0]].name==FIELD:return {'error':{'code':-32000}}
        return original(method,args)
    a.rpc_request=rpc
    with pytest.raises(ChainUnavailable):snapshot(a)

def test_zero_spot_is_a_successful_value_but_sdk_contradiction_refuses():
    a,b=EmaSDK(),EmaSDK();a.spot[3]=b.spot[3]=0
    assert snapshot(a,b)['prices']['3']['spot']['rao']==0
    a.runtime_call=lambda *args,**kwargs:None
    with pytest.raises(SourceLied):snapshot(a,b)

def test_reference_hash_and_root_request_refuse():
    c=chain(EmaSDK(),EmaSDK())
    with pytest.raises(SourceLied):c.ema_price_snapshot(50,[2],expected_block_hash=h(49),ema_source=FIELD)
    with pytest.raises(SourceLied):c.ema_price_snapshot(50,[0,2],expected_block_hash=h(50),ema_source=FIELD)

@pytest.mark.parametrize('n',[-2**127,-1,0,141,36045255,2**127-1])
def test_exact_signed128_encoding(n):assert fixed('0x'+n.to_bytes(16,'little',signed=True).hex(),True)==n


def test_absent_sdk_boolean_is_not_integer_zero():
    a=EmaSDK();a.ema[3]=None;original=a.query_multi
    def query(keys,*,block_hash):
        rows=original(keys,block_hash=block_hash)
        if keys[0].name==FIELD:return [(k,{'bits':False} if k.asset==3 else v) for k,v in rows]
        return rows
    a.query_multi=query
    with pytest.raises(SourceLied):snapshot(a)
