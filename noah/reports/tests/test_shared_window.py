"""Shared-window proof tests. Only synthetic RPC transport replaces a boundary.

The real SCALE SDK, collector, target engines, context selector, raw offline
replay and inventory checks execute. No live chain, wallets or signing keys.
"""
import copy
import gzip
import hashlib
import json
import socket
from unittest.mock import patch

import pytest
from substrateinterface import Keypair

from noah.reports.adjudication import adjudicate, verify_adjudication, _inventory
from noah.reports.artifacts import canonical_bytes
from noah.reports.budget import ProviderBudget, BoundedSocket
from noah.reports.common import digest, ReportError
from noah.reports.package_inventory import ReportPackageRefused
from noah.reports.shared_window import ENGINE_SCHEMA as SCHEMA
from noah.reports.backing.chain_session import commitment_info
from noah.reports.backing.constants import PERQUINTILL_ONE
from noah.reports.backing.engine import compute
from noah.reports.backing.protocol.context_commitment import build_commitment
from noah.reports.backing.protocol.preimage import ReportSubmission
from noah.reports.backing.rpc import result_sha256
from noah.reports.backing.offline import EvidenceError
from noah.reports.backing.source import ReplayError
from noah.reports.tests.raw_export_fixture import RawChain, RawSocket, block_hash, HOT, COLD, ARK


class MultiRawChain(RawChain):
    def __init__(self, targets=(82,83,84)):
        super().__init__(context=True)
        self.contracts=[self.contract]
        for target in targets[1:]:
            self.engine.set_pool(target,alpha_in=50000000000000,tao=50000000000000,
                alpha_out=2000000000000,quote=PERQUINTILL_ONE//2)
            from noah.reports.tests.fakes import alpha_storage_key
            from noah.reports.backing.keys import hex_encode
            self.engine.alpha_keys.append(hex_encode(alpha_storage_key(b'\x99'*32,b'\x88'*32,target)))
            contract={**self.contract,'reported_netuid':target,'release_prefix':f'{target:032x}'}
            self.contracts.append(contract)
            values,_=compute(self,target,block_hash(2),2,ark=ARK)
            assert values['status']=='OK'
            report=ReportSubmission(bytes.fromhex(contract['genesis_hash']),HOT,COLD,target,82,20261115,
                2,bytes.fromhex(block_hash(2)[2:]),values['output1']['drop_ppm'],
                values['output2']['alpha_required_rao'],values['output3']['alpha_required_rao'],
                bytes.fromhex(contract['release_prefix']),b's'*16).encode()
            variant,commitment='BigRaw',build_commitment(contract,report)
            for number,kind,payload in ((3,variant,commitment),(5,'BigRaw',report)):
                call=self.sdk.compose_call('Commitments','set_commitment',
                    {'netuid':82,'info':commitment_info(kind,payload)})
                ext=self.sdk.create_signed_extrinsic(call,Keypair(public_key=HOT,crypto_type=1,ss58_format=42),
                    nonce=0,signature=bytes(64))
                self.blocks[block_hash(number)]['block']['extrinsics'].append(str(ext.data))
        value_type=self.sdk.create_storage_key('System','Events',[]).value_scale_type
        for number in (3,5):
            events=[{'phase':{'ApplyExtrinsic':index},'event':{'System':{'ExtrinsicSuccess':{
                'dispatch_info':{'weight':{'ref_time':0,'proof_size':0},'class':'Normal','pays_fee':'Yes'}}}},'topics':[]}
                for index in range(1,len(targets)+1)]
            obj=self.sdk.runtime_config.create_scale_object(value_type,metadata=self.sdk.metadata)
            self.events[self.event_key][block_hash(number)]=str(obj.encode(events))

    def call(self,method,params):
        if method=='state_call' and params[0]=='StakeInfoRuntimeApi_get_stake_availability_for_coldkeys':
            self.engine.netuid=int.from_bytes(bytes.fromhex(params[1][2:])[-2:],'little')
        return super().call(method,params)

    def request(self):
        request=super().request()
        request['policy']['required_rounds']=self.contracts
        request['policy_sha256']=digest(request['policy'])
        request['assignments']=[dict(uid=0,hotkey=HOT.hex(),coldkey=COLD.hex(),registration_block=1,
                                    targets=[row['reported_netuid'] for row in self.contracts[:3]])]
        return request


def produce(root, readers, *, legacy=False):
    request=readers['reader_a'].request()
    invocation=dict(schema='noah.report-adjudication-invocation/1',request=request,sources=[
        dict(id=label,endpoint=f'ws://{label}.invalid',history=None) for label in readers])
    def raw_socket(budget,endpoint):
        from types import SimpleNamespace
        raw=RawSocket(readers[endpoint.split('//')[1].split('.')[0]])
        value=BoundedSocket(endpoint,budget)
        value.connection=SimpleNamespace(send=raw.send, recv=lambda **kwargs:raw.recv(), close=raw.close)
        budget.connections.append(value)
        return value
    from contextlib import nullcontext
    selection=patch('noah.reports.shared_window.eligible',return_value=False) if legacy else nullcontext()
    with selection, patch.object(ProviderBudget,'socket',raw_socket), \
            patch.object(socket.socket,'connect',side_effect=AssertionError('real network forbidden')):
        result=adjudicate(invocation,root)
    return request,result


@pytest.fixture(scope='module')
def paired(tmp_path_factory):
    root=tmp_path_factory.mktemp('shared-window')
    readers={label:MultiRawChain() for label in ('reader_a','reader_b')}
    counts=[]
    for legacy,name in ((True,'v1'),(False,'v3')):
        for reader in readers.values():reader.calls.clear()
        request,result=produce(root/name,readers,legacy=legacy)
        counts.append(sum(sum(method=='chain_getBlock' for method,_ in r.calls) for r in readers.values()))
        (root/(name+'-result.json')).write_bytes(canonical_bytes(result))
    return root,request,counts


def normalized(result):
    result=copy.deepcopy(result)
    result.pop('manifest_sha256')
    for row in result['rounds']:row.pop('readers')
    return result


def test_three_targets_same_verdicts_raw_replay_and_less_chain_work(paired):
    root,request,counts=paired
    a=json.loads((root/'v1-result.json').read_bytes())
    b=json.loads((root/'v3-result.json').read_bytes())
    assert normalized(a)==normalized(b)
    assert b['status']=='COMPLETE' and b['scored_decisions'][0]['outcome']=='verified'
    assert all(row['outcome']=='verified' for row in b['scored_decisions'][0]['rounds'])
    assert counts[1]*3 < counts[0]  # Shared history plus immutable RPC reuse eliminates duplicate reads.
    replay_b=verify_adjudication(root/'v3',request,b['manifest_sha256'],include_round_artifacts=True)
    replay_a=verify_adjudication(root/'v1',request,a['manifest_sha256'],include_round_artifacts=True)
    assert replay_b.pop('round_artifacts')==replay_a.pop('round_artifacts')
    assert replay_b==b and replay_a==a
    assert json.loads((root/'v3/manifest.json').read_bytes())['schema']==SCHEMA
    assert not list((root/'v3').rglob('block-*.json'))
    assert len(list((root/'v3').rglob('metadata.hex')))==2
    assert len(list((root/'v3').rglob('coverage.json')))==2
    assert not list((root/'v3').rglob('evidence.json'))


def repin(root):
    for path in root.glob('shared/*/manifest.json'):
        doc=json.loads(path.read_bytes());doc['files']=_inventory(path.parent)
        path.write_bytes(canonical_bytes(doc))
    for path in root.glob('rounds/*/reader_*/manifest.json'):
        doc=json.loads(path.read_bytes());doc['files']=_inventory(path.parent)
        shared=root/'shared'/path.parent.name/'manifest.json'
        doc['shared_manifest_sha256']=hashlib.sha256(shared.read_bytes()).hexdigest()
        path.write_bytes(canonical_bytes(doc))
    path=root/'manifest.json';doc=json.loads(path.read_bytes());doc['files']=_inventory(root)
    path.write_bytes(canonical_bytes(doc))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('kind',['raw','reader','binding','target','missing','extra','coverage','snapshot','engine-pin','engine-missing','engine-unpinned','engine-reader','schema','response-sha','response-size','response-empty'])
def test_rehashed_shared_tampering_never_authorizes_saved_verdict(paired,tmp_path,kind):
    import shutil
    original,request,_=paired
    root=tmp_path/'package';shutil.copytree(original/'v3',root)
    if kind.startswith('response-'):
        path=root/'shared/reader_a/responses-0000.jsonl.gz'
        rows=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        if kind=='response-sha':rows[0]['sha256']='0'*64
        elif kind=='response-size':rows[0]['byte_length']+=1
        else:rows=[]
        path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows),mtime=0))
    elif kind.startswith('engine-'):
        path=root/'shared/reader_a/engine-0000.jsonl.gz'
        rows=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        target=next(row for row in rows if row['method']=='state_getKeysPaged')
        if kind=='engine-pin':target['params'][-1]='0x'+'ab'*32
        elif kind=='engine-missing':rows.remove(target)
        elif kind=='engine-unpinned':target['method']='chain_getHead';target['params']=[]
        else:
            target['result']=[]
            target['sha256'],target['byte_length']=result_sha256(target['result'])
        path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows),mtime=0))
    elif kind=='schema':
        path=root/'manifest.json';doc=json.loads(path.read_bytes());doc['schema']='noah.report-adjudication-manifest/2'
        path.write_bytes(canonical_bytes(doc))
    elif kind=='raw':
        path=root/'shared/reader_a/responses-0000.jsonl.gz'
        rows=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        for row in rows:
            if row['method']=='chain_getBlock' and row['params']==[block_hash(3)]:
                row['result']['block']['extrinsics']=row['result']['block']['extrinsics'][:1]
                row['sha256'],row['byte_length']=result_sha256(row['result'])
        path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows),mtime=0))
    elif kind=='snapshot':
        from noah.reports.backing.keys import hex_encode, blake2_128_concat
        from noah.reports.roster import _prefix
        path=root/'shared/reader_a/responses-0000.jsonl.gz'
        rows=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        owner=hex_encode(_prefix('Owner')+blake2_128_concat(HOT))
        changed=0
        for row in rows:
            if row['method'] in ('state_getStorageAt','state_getStorage') and row['params'][0]==owner:
                row['result']='0x'+'cc'*32
                row['sha256'],row['byte_length']=result_sha256(row['result']);changed+=1
        assert changed
        path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows),mtime=0))
    elif kind=='coverage':
        path=root/'shared/reader_a/coverage.json';rows=json.loads(path.read_bytes())
        rows[0]['timestamp_ms']+=1;path.write_bytes(canonical_bytes(rows))
    elif kind=='reader':
        path=root/'shared/reader_a/manifest.json';doc=json.loads(path.read_bytes())
        doc['reader']='reader_b';path.write_bytes(canonical_bytes(doc))
    elif kind=='binding':
        path=root/'shared/reader_a/manifest.json';doc=json.loads(path.read_bytes())
        doc['binding']['evaluation']['block_hash']='ab'*32;path.write_bytes(canonical_bytes(doc))
    elif kind=='target':
        path=next(root.glob('rounds/*/reader_a/collection/round.json'))
        doc=json.loads(path.read_bytes());doc['reported_netuid']=100;path.write_bytes(canonical_bytes(doc))
    elif kind=='missing':
        (root/'shared/reader_a/responses-0000.jsonl.gz').unlink()
    else:
        (root/'shared/reader_a/unexpected.json').write_bytes(b'{}')
    sha=repin(root)
    with pytest.raises((ValueError,ReportError,ReportPackageRefused,EvidenceError,ReplayError)):
        verify_adjudication(root,request,sha)


def test_package3_manifest_identity_labels(paired):
    from noah.reports.identity_shapes import validate_adjudication,validate_shared_window
    root,request,_=paired;package=root/'v3'
    doc=json.loads((package/'manifest.json').read_bytes());validate_adjudication(doc)
    validate_shared_window(json.loads((package/'shared/reader_a/manifest.json').read_bytes()))
    assert all(row['kind']=='rpc_responses' for row in doc['files'] if '/responses-' in row['path'])
