"""Assignment-only trust, durable files and unsigned read worker; synthetic only."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import URLError

import pytest
import sr25519

from noah.reports import assignments as a
from noah.reports import assignment_worker as w
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.verification import CALCULATION


@pytest.fixture
def case():
    pair = sr25519.pair_from_seed(b'a' * 32)
    key = pair[0].hex()
    template = dict(schema=a.TEMPLATE, genesis_hash='11' * 32, mining_netuid=2,
                    day_anchor_ts=1794700800, effective_day=0, snapshot_offset_seconds=600,
                    prepare_lead_seconds=600, commit_seconds=7200, reveal_seconds=7200,
                    eligibility_policy_version=REPORT_POLICY, calculation_version=CALCULATION,
                    scoring_release='synthetic', ark_coldkey='22' * 32, ark_validator_hotkey='33' * 32,
                    assignment_signer_public_key=key, commitment_format='context-v1')
    roster = [dict(uid=0, hotkey='44' * 32, coldkey='55' * 32, registration_block=2)]
    unsigned = a.build_week(template, roster, [1, 2, 3], week_start='2026-11-15',
                            roster_block_number=10, roster_block_hash='ef' * 32)
    signed = a.sign_week(unsigned, pair, trusted_public_key=key)
    identity = {k: template[k] for k in a.IDENTITY_FIELDS}
    return SimpleNamespace(pair=pair, key=key, template=template, roster=roster,
                           unsigned=unsigned, signed=signed, identity=identity)


def test_native_domain_signature_and_required_independent_trust(case):
    assert a.verify_week(case.signed, trusted_public_key=case.key) == case.signed
    with pytest.raises(TypeError):
        a.verify_week(case.signed)
    with pytest.raises(a.ReportError, match='trusted key'):
        a.verify_week(case.signed, trusted_public_key='aa' * 32)
    raw_signed = {**case.unsigned, 'signature': sr25519.sign(case.pair, a.canonical_bytes(case.unsigned)).hex()}
    with pytest.raises(a.ReportError, match='signature'):
        a.verify_week(raw_signed, trusted_public_key=case.key)
    assert case.key != case.template['ark_validator_hotkey']


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(schema='noah.weekly-assignments/1'),
    lambda d: d.update(publisher_hotkey='33' * 32),
    lambda d: d.update(commitment_format='legacy'),
    lambda d: d.update(roster_block_number=True),
    lambda d: d.update(roster_block_hash='GG' * 32),
    lambda d: d['assignments'][0].update(registration_block=11),
    lambda d: d['assignments'][0].update(uid=True),
    lambda d: d['assignments'][0].update(hotkey='ab'),
    lambda d: d['assignments'].append(copy.deepcopy(d['assignments'][0])),
    lambda d: d.update(week_start='2026-11-16'),
])
def test_crossed_or_malformed_signed_body_refuses(case, mutate):
    d = copy.deepcopy(case.signed); mutate(d)
    with pytest.raises(a.ReportError):
        a.verify_week(d, trusted_public_key=case.key)


def test_valid_cache_only_survives_expected_network_outages(case, tmp_path, monkeypatch):
    a.cache_document(tmp_path, case.signed, trusted_public_key=case.key)
    now = datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(a, 'urlopen', lambda *args, **kwargs: (_ for _ in ()).throw(URLError('synthetic outage')))
    assert a.current_document(tmp_path, 'https://invalid.example/', now,
                              trusted_public_key=case.key, expected_template_identity=case.identity) == case.signed
    with pytest.raises(a.ReportError, match='different week'):
        path = tmp_path / 'weekly-assignments/2026-11-22.json'
        path.write_text(json.dumps(case.signed))
        a.current_document(tmp_path, None, now + 7 * 86400,
                           trusted_public_key=case.key, expected_template_identity=case.identity)


@pytest.mark.parametrize('field,value', [('genesis_hash','66'*32), ('mining_netuid',82),
                                       ('ark_coldkey','77'*32), ('ark_validator_hotkey','88'*32)])
def test_bound_identity_checked_before_cache(case, tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(a, 'fetch_document', lambda *args, **kwargs: case.signed)
    expected = {**case.identity, field:value}
    with pytest.raises(a.ReportError, match='identity differs'):
        a.current_document(tmp_path, 'https://invalid.example/', 1794950000,
                           trusted_public_key=case.key, expected_template_identity=expected)
    assert not (tmp_path / 'weekly-assignments').exists()


def test_invalid_remote_signature_does_not_silently_fall_back(case, tmp_path, monkeypatch):
    a.cache_document(tmp_path, case.signed, trusted_public_key=case.key)
    bad = {**case.signed, 'signature':'00'*64}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _): return json.dumps(bad).encode()
    monkeypatch.setattr(a, 'urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(a.ReportError, match='signature'):
        a.current_document(tmp_path, 'https://invalid.example/', 1794950000,
                           trusted_public_key=case.key, expected_template_identity=case.identity)


def test_atomic_retry_keeps_first_signature_and_conflict_refuses(case, tmp_path):
    a.retain_week(tmp_path, case.signed, trusted_public_key=case.key)
    path = tmp_path / '2026-11-15.json'; before = path.read_bytes()
    resigned = a.sign_week(case.unsigned, case.pair, trusted_public_key=case.key)
    assert a.retain_week(tmp_path, resigned, trusted_public_key=case.key) == case.signed
    assert path.read_bytes() == before
    other = a.build_week(case.template, case.roster, [1,2,4], week_start='2026-11-15',
                         roster_block_number=10, roster_block_hash='ef'*32)
    with pytest.raises(a.ReportError, match='retained week file differs'):
        a.retain_week(tmp_path, a.sign_week(other, case.pair, trusted_public_key=case.key), trusted_public_key=case.key)
    assert path.read_bytes() == before


def test_interrupted_temp_write_leaves_no_partial_final_and_retry_succeeds(case, tmp_path, monkeypatch):
    from noah.reports.backing import prepare
    original = prepare.os.write
    def partial(fd, data):
        original(fd, data[:30]); raise OSError('synthetic disk interruption')
    with monkeypatch.context() as m:
        m.setattr(prepare.os, 'write', partial)
        with pytest.raises(OSError):
            a.retain_week(tmp_path, case.signed, trusted_public_key=case.key)
    assert not (tmp_path / '2026-11-15.json').exists()
    assert not list(tmp_path.glob('*.tmp'))
    a.retain_week(tmp_path, case.signed, trusted_public_key=case.key)
    assert a.verify_week(a._read_document(tmp_path / '2026-11-15.json'), trusted_public_key=case.key)


def test_completed_link_before_temp_unlink_is_readable_retry(case, tmp_path, monkeypatch):
    from noah.reports.backing import prepare
    original = prepare.os.unlink
    def interrupt(path, *args, **kwargs):
        if str(path).endswith('.tmp'): raise OSError('synthetic crash after link')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(prepare.os, 'unlink', interrupt)
        with pytest.raises(OSError): a.retain_week(tmp_path, case.signed, trusted_public_key=case.key)
    assert a.retain_week(tmp_path, case.signed, trusted_public_key=case.key) == case.signed


@pytest.mark.parametrize('kind', ['public', 'group_write', 'symlink', 'hardlink', 'long', 'wrong_key'])
def test_seed_file_boundaries(case, tmp_path, kind):
    path=tmp_path/'assignment.seed'; path.write_bytes(b'a'*32); path.chmod(0o600)
    if kind=='public': path.chmod(0o644)
    if kind=='group_write': path.chmod(0o660)
    if kind=='long': path.write_bytes(b'a'*33)
    if kind=='symlink':
        alias=tmp_path/'alias';alias.symlink_to(path);path=alias
    if kind=='hardlink': os.link(path,tmp_path/'alias')
    key='aa'*32 if kind=='wrong_key' else case.key
    with pytest.raises(a.ReportError): a.load_assignment_key(path,trusted_public_key=key)


def test_seed_file_and_primary_verifier_need_no_old_sdk(case, tmp_path):
    path=tmp_path/'assignment.seed';path.write_bytes(b'a'*32);path.chmod(0o600)
    assert a.load_assignment_key(path,trusted_public_key=case.key)==case.pair
    source=Path(a.__file__).resolve().parents[2]
    code='''import sys, json, importlib.abc
sys.path.insert(0,sys.argv[1])
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name.split('.')[0] in ('substrateinterface','scalecodec','bittensor','bittensor_wallet'): raise AssertionError(name)
sys.meta_path.insert(0,Block())
from noah.reports.assignments import verify_week
verify_week(json.loads(sys.argv[2]),trusted_public_key=sys.argv[3])
print('native-only verified')
'''
    done=subprocess.run([sys.executable,'-I','-B','-c',code,str(source),json.dumps(case.signed),case.key],capture_output=True,text=True,check=True)
    assert done.stdout=='native-only verified\n'


class Chain:
    """Raw runtime483 data for one registered miner and one healthy pool."""
    def __init__(self):
        from noah.reports.backing import keys as k
        from noah.reports.roster import _prefix
        hot=bytes.fromhex('44'*32)
        self.rows={k.total_networks_key():(2).to_bytes(2,'little'),
                   k.networks_added_key(1):b'\x01', k.networks_added_key(2):b'\x01',
                   k.subnet_mechanism_key(1):b'\x01\x00', k.subtoken_enabled_key(1):b'\x01',
                   k.pal_swap_initialized_key(1):b'\x01',
                   k.subnet_alpha_in_key(1):(10**9).to_bytes(8,'little'),
                   k.subnet_tao_key(1):(10**9).to_bytes(8,'little'),
                   _prefix('SubnetworkN',2):(1).to_bytes(2,'little'),
                   _prefix('Keys',2)+b'\x00\x00':hot,
                   _prefix('Uids',2)+k.blake2_128_concat(hot):b'\x00\x00',
                   _prefix('Owner')+k.blake2_128_concat(hot):bytes.fromhex('55'*32),
                   _prefix('BlockAtRegistration',2)+b'\x00\x00':(2).to_bytes(8,'little')}
        self.calls=[]
    def call(self,method,params):
        from noah.reports.backing import keys as k
        from noah.reports.backing.constants import RUNTIME_483_CODE_HASH
        self.calls.append((method,params))
        if method=='chain_getBlockHash':
            n=int(params[0],16) if isinstance(params[0],str) else params[0]
            return '0x'+('11'*32 if n==0 else format(n,'064x'))
        if method=='chain_getFinalizedHead':return self.call('chain_getBlockHash',[22000])
        if method=='chain_getHeader':return {'number':hex(int(params[0],16))}
        if method=='state_getRuntimeVersion':return {'specVersion':483,'transactionVersion':1}
        if method=='state_getStorageHash':return RUNTIME_483_CODE_HASH
        if method=='state_getKeysPaged':
            return sorted('0x'+key.hex() for key in self.rows if ('0x'+key.hex()).startswith(params[0]) and '0x'+key.hex()>params[2])[:params[1]]
        if method=='state_getStorage':
            key=bytes.fromhex(params[0][2:])
            value=((1794700800+(int(params[1],16)-10)*12)*1000).to_bytes(8,'little') if key==k.timestamp_now_key() else self.rows.get(key)
            return None if value is None else '0x'+value.hex()
        raise AssertionError(method)


def test_unsigned_worker_runtime483_real_roster_and_target_reader(case):
    source=Chain();result=w._build(case.template,source)
    assert result['week']['targets']==[1]
    assert result['week']['roster_block_number']==10
    assert result['week']['assignments'][0]['registration_block']==2
    assert result['week']['week_start']=='2026-11-15'
    assert 'signature' not in result['week']
    assert sum(method=='state_getRuntimeVersion' for method,_ in source.calls)==2
    assert not any(method.startswith('author_') for method,_ in source.calls)


def test_worker_uses_bounded_read_transport_and_closes(case, monkeypatch):
    source=Chain();seen={}
    class Budget:
        def __init__(self,**kwargs):seen.update(kwargs)
        def transport(self,endpoint,payload,timeout):return {'result':source.call(payload['method'],payload['params'])}
        def close(self):seen['closed']=True
    monkeypatch.setattr(w,'ProviderBudget',Budget)
    result=w.build(dict(schema=w.SCHEMA,template=case.template,endpoint='ws://synthetic.invalid'))
    assert result['schema']==w.RESULT and seen['closed'] is True
    assert seen['seconds']==120 and seen['calls']==20000 and seen['total_bytes']==128*1024**2


def test_schedule_retains_context_commitment_format(case):
    from noah.reports.schedule import validate_schedule
    schedule=a.schedule_for_target(a.template_from_document(case.template),1)
    assert schedule['commitment_format']=='context-v1'
    assert 'assignment_signer_public_key' not in schedule
    assert validate_schedule(schedule)


def test_complete_max_roster_fits_primary_assignment_response(case):
    roster=[dict(uid=i,hotkey=format(i+1,'064x'),coldkey='55'*32,registration_block=2)
            for i in range(a.MAX_MEMBERS)]
    unsigned=a.build_week(case.template,roster,list(range(1,129)),week_start='2026-11-15',
                          roster_block_number=10,roster_block_hash='ef'*32)
    response=dict(schema=w.RESULT,week=unsigned,finalized=dict(block_number=22000,block_hash='aa'*32,timestamp_ms=1794964680000))
    assert len(json.dumps(response,indent=2).encode())<2*1024**2


def test_publication_lock_contention_writes_no_final(case,tmp_path):
    with a._locked(tmp_path):
        with pytest.raises(BlockingIOError):
            a.retain_week(tmp_path,case.signed,trusted_public_key=case.key)
    assert not (tmp_path/'2026-11-15.json').exists()


@pytest.mark.parametrize('wrong', ['genesis','code','runtime','finalized'])
def test_worker_refuses_crossed_chain_identity(case,wrong):
    source=Chain();original=source.call
    def call(method,params):
        if wrong=='genesis' and method=='chain_getBlockHash' and params==[0]:return '0x'+'22'*32
        if wrong=='code' and method=='state_getStorageHash':return '0x'+'00'*32
        if wrong=='runtime' and method=='state_getRuntimeVersion':return {'specVersion':999999,'transactionVersion':1}
        if wrong=='finalized' and method=='chain_getBlockHash' and params==[22000]:return '0x'+'aa'*32
        return original(method,params)
    source.call=call
    with pytest.raises((ValueError,a.ReportError)):
        w._build(case.template,source)


def test_worker_rejects_signing_inputs_and_closes_on_failure(case,monkeypatch):
    invocation=dict(schema=w.SCHEMA,template=case.template,endpoint='ws://synthetic.invalid')
    with pytest.raises(a.ReportError,match='fields'):
        w.build({**invocation,'signing_key_file':'/not/read'})
    seen=[]
    class Budget:
        def __init__(self,**kwargs):pass
        def transport(self,*args):raise OSError('synthetic transport failure')
        def close(self):seen.append('closed')
    monkeypatch.setattr(w,'ProviderBudget',Budget)
    with pytest.raises(OSError):w.build(invocation)
    assert seen==['closed']


@pytest.mark.parametrize('field',['ark_coldkey','ark_validator_hotkey'])
def test_assignment_authority_must_be_separate_from_ark_custody(case,field):
    template={**case.template,'assignment_signer_public_key':case.template[field]}
    with pytest.raises(a.ReportError,match='separate'):
        a.validate_template(template)


def test_root_may_read_own_group_readable_service_key(case,tmp_path,monkeypatch):
    path=tmp_path/'assignment.seed';path.write_bytes(b'a'*32);path.chmod(0o640)
    # Simulate only ownership identity; real bytes/stat mode/path stay exercised.
    monkeypatch.setattr(a.os,'geteuid',lambda:0)
    # The real owner must still be root: this special allowance is meaningful
    # on Linux root CI; nonroot local executions retain the ordinary owner check.
    if path.stat().st_uid != 0:
        with pytest.raises(a.ReportError,match='parent permissions'):
            a.load_assignment_key(path,trusted_public_key=case.key)
    else:
        assert a.load_assignment_key(path,trusted_public_key=case.key)==case.pair


@pytest.mark.parametrize('spec', [483, 490])
def test_sparse_subnet_ids_use_complete_pinned_storage_keys(spec):
    from noah.reports.backing import keys as k
    from noah.reports.backing.reads import Reader
    source = Chain()
    source.rows[k.total_networks_key()] = (4).to_bytes(2, 'little')
    source.rows[k.networks_added_key(7)] = b'\x01'
    for make in (k.subnet_mechanism_key, k.subtoken_enabled_key,
                 k.pal_swap_initialized_key, k.subnet_alpha_in_key, k.subnet_tao_key):
        source.rows[make(7)] = source.rows[make(1)]
    if spec == 490:
        for n in (1, 2, 7): source.rows[k.subnet_state_key(n)] = b'\x01'
    original = source.call
    def call(method, params):
        if method == 'state_getRuntimeVersion': return {'specVersion': spec}
        # Force small pages: the consumer must continue even below requested size.
        result = original(method, params)
        return result[:1] if method == 'state_getKeysPaged' else result
    source.call = call
    reader = Reader(source, '0x' + 'aa' * 32)
    assert a.supported_targets(reader) == [1, 7]
    pages = [p for m, p in source.calls if m == 'state_getKeysPaged']
    assert len(pages) == 4 and all(p[-1] == reader.block_hash for p in pages)


@pytest.mark.parametrize('bad', ['duplicate', 'foreign', 'malformed', 'oversized'])
def test_subnet_enumeration_refuses_malformed_or_nonprogressing_pages(bad):
    from noah.reports.backing import keys as k
    from noah.reports.backing.reads import Reader
    source = Chain(); original = source.call
    def call(method, params):
        if method != 'state_getKeysPaged': return original(method, params)
        item = '0x' + k.networks_added_key(1).hex()
        return {'duplicate': [item, item], 'foreign': ['0x' + '00' * 34],
                'malformed': [item[:-1] + 'z'], 'oversized': [item] * 257}[bad]
    source.call = call
    with pytest.raises(a.ReportError): a.supported_targets(Reader(source, '0x' + 'aa' * 32))


def test_weekly_issuer_uses_recent_state_and_keeps_original_roster_and_week(case, monkeypatch):
    from noah.reports import schedule
    from noah.reports.backing.keys import timestamp_now_key
    class RecentChain(Chain):
        def __init__(self, earliest=0):
            super().__init__()
            self.earliest, self.timestamp_reads = earliest, []
        def call(self, method, params):
            if method == 'chain_getFinalizedHead':
                return self.call('chain_getBlockHash', [9_540_000])
            if method == 'state_getStorage' and params[0] == '0x' + timestamp_now_key().hex():
                number = int(params[1], 16)
                self.timestamp_reads.append(number)
                if number < self.earliest:
                    raise a.ReportError('synthetic historical state unavailable')
                stamp = (1794700800 + (number - 9_500_000) * 12) * 1000
                return '0x' + stamp.to_bytes(8, 'little').hex()
            return super().call(method, params)
    # The original issuer searched the full [1, head] interval. Only the search
    # range differs; week construction and snapshot/roster checks stay real.
    with monkeypatch.context() as old:
        old.setattr(schedule, '_recent_range', lambda block, head, target: (1, head))
        original = w._build(case.template, RecentChain())
    source = RecentChain(earliest=9_440_000)
    current = w._build(case.template, source)
    assert current == original
    assert current['week']['roster_block_number'] == 9_500_000
    assert current['week']['week_start'] == '2026-11-15'
    assert min(source.timestamp_reads) == 9_540_000 - 65536
    with pytest.raises(a.ReportError, match='historical state unavailable'):
        w._build(case.template, RecentChain(earliest=9_500_000))
