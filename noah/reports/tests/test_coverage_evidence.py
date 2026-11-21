"""Fresh coverage admission and exact historical replay share unchanged answers."""
import copy
import json
from pathlib import Path
import pytest
from noah.reports.backing.constants import COVERAGE_LEGACY, COVERAGE_STAKED_V2, RUNTIME_CODE_PINS
from noah.reports.backing.engine import compute, replay_compute, _compute
from noah.reports.backing.keys import (hex_encode, networks_added_key, total_alpha_staked_key,
    total_alpha_staked_complete_key, total_alpha_staked_migration_key)
from noah.reports.backing.scale import encode_u64
from noah.reports.backing.rpc import RpcClient
from noah.reports.backing.source import LiveSource, ReplaySource
from noah.reports.identity_shapes import validate_engine
from noah.reports.artifacts import canonical_bytes, engine_binding, retained_coverage_method
from noah.reports.tests.test_engine_arithmetic import _chain, _add, BLOCK, NETUID
from noah.reports.tests.sample import fixture


def chain(spec=492, *, total=1001, complete='0x01', cursor=None):
    c=_chain();c.spec=spec
    _add(c,b'\x22'+bytes(31),1000,0,1000)
    c.storage.update({hex_encode(networks_added_key(NETUID)):'0x01',
        hex_encode(total_alpha_staked_key(NETUID)):hex_encode(encode_u64(total)),
        hex_encode(total_alpha_staked_complete_key()):complete,
        hex_encode(total_alpha_staked_migration_key()):cursor})
    original=c.call
    def call(method,params):
        if method=='state_getStorageHash':
            c.calls.append((method,params));assert params==['0x3a636f6465',BLOCK]
            return RUNTIME_CODE_PINS.get(spec)
        return original(method,params)
    c.call=call
    return c


def retained(tmp_path,engine):
    contract=fixture()['round']
    contract.update(block_hash=BLOCK[2:],block_number=engine['block_number'],reported_netuid=NETUID)
    if engine['status']=='OK':contract['ark_coldkey']=engine['ark_coldkey'][2:]
    tmp_path.mkdir(parents=True,exist_ok=True)
    (tmp_path/'engine.json').write_bytes(canonical_bytes(engine))
    (tmp_path/'engine-binding.json').write_bytes(canonical_bytes(engine_binding(contract,engine)))
    return contract


@pytest.mark.parametrize('spec',[483,492,493])
def test_fresh_coverage_uses_stake_not_nonholder_alpha_and_answers_are_identical(spec):
    c=chain(spec)
    new=compute(c,NETUID,BLOCK)[0];old=_compute(c,NETUID,BLOCK)[0]
    assert new['coverage_method']==COVERAGE_STAKED_V2
    assert new['coverage']['basis']==COVERAGE_STAKED_V2
    assert new['coverage']['stake_pool_total_rao']==1001
    assert new['coverage']['gap_rao']==1
    assert not new['coverage']['coverage_gap_exceeds_holder']
    assert old['coverage']['coverage_gap_exceeds_holder']
    for field in ('output1','output2','output3','holder','pool','ark_context'):
        assert new[field]==old[field]
    validate_engine(new);validate_engine(old)


@pytest.mark.parametrize('complete,cursor',[(None,None),('0x00',None),('0x02',None),('0x0100',None),('0x01','0x00')])
def test_fresh_missing_migration_never_retries_legacy(complete,cursor):
    c=chain(complete=complete,cursor=cursor)
    out=compute(c,NETUID,BLOCK)[0]
    assert out['status']=='UNAVAILABLE' and out['reason']=='stake_coverage_migration_incomplete'
    assert out['coverage_method']==COVERAGE_STAKED_V2
    assert not any(m=='state_getKeysPaged' for m,_ in c.calls)
    validate_engine(out)


@pytest.mark.parametrize('spec',[490,491])
def test_fresh_old_runtime_without_reviewed_cache_refuses(spec):
    out=compute(chain(spec),NETUID,BLOCK)[0]
    assert out['reason']=='stake_coverage_runtime_unsupported'
    assert out['coverage_method']==COVERAGE_STAKED_V2


@pytest.mark.parametrize('total',[0,999])
def test_fresh_observed_above_pool_is_not_clamped(total):
    out=compute(chain(total=total),NETUID,BLOCK)[0]
    assert out['reason']=='stake_coverage_observed_exceeds_pool'
    assert out['coverage_method']==COVERAGE_STAKED_V2


def test_missing_holder_bound_remains_strict():
    out=compute(chain(total=2001),NETUID,BLOCK)[0]
    assert out['coverage']['gap_rao']==1001 and out['coverage']['coverage_gap_exceeds_holder']


def test_fresh_entry_has_no_legacy_selector_and_replay_refuses_live_sources():
    with pytest.raises(TypeError):compute(chain(),NETUID,BLOCK,coverage_method=COVERAGE_LEGACY)
    with pytest.raises(ValueError,match='retained'):replay_compute(chain(),NETUID,BLOCK,coverage_method=COVERAGE_LEGACY)


@pytest.mark.parametrize('legacy',[False,True])
def test_both_versions_replay_exact_raw_receipts_and_binding(tmp_path,legacy):
    c=chain()
    def transport(endpoint,request,timeout):return {'result':c.call(request['method'],request['params'])}
    client=RpcClient(['ws://synthetic.invalid'],BLOCK,transport)
    original=(_compute if legacy else compute)(LiveSource(client),NETUID,BLOCK)[0]
    contract=retained(tmp_path,original)
    method=retained_coverage_method(tmp_path,contract)
    assert method==(COVERAGE_LEGACY if legacy else COVERAGE_STAKED_V2)
    rows=[r.to_json() for r in client.receipts]
    rebuilt=replay_compute(ReplaySource(rows,BLOCK),NETUID,BLOCK,coverage_method=method)[0]
    assert canonical_bytes(rebuilt)==canonical_bytes(original)
    cachekeys={hex_encode(total_alpha_staked_key(NETUID)),hex_encode(total_alpha_staked_complete_key()),hex_encode(total_alpha_staked_migration_key())}
    calls=[r for r in client.receipts if r.method=='state_getStorage' and r.params[0] in cachekeys]
    assert len(calls)==(0 if legacy else 3)


@pytest.mark.parametrize('mutation',['strip','unknown','mixed','bad_pool'])
def test_changed_marker_or_new_shape_cannot_select_legacy(tmp_path,mutation):
    out=compute(chain(),NETUID,BLOCK)[0]
    if mutation=='strip':out.pop('coverage_method')
    if mutation=='unknown':out['coverage_method']='unknown'
    if mutation=='mixed':out['coverage']['basis']='alpha-out-v1'
    if mutation=='bad_pool':out['coverage']['stake_pool_total_rao']=True
    contract=retained(tmp_path,out)
    with pytest.raises(ValueError):retained_coverage_method(tmp_path,contract)


def test_stripped_new_shape_retaining_corrected_gap_cannot_replay_old_arithmetic(tmp_path):
    c=chain()
    out=compute(c,NETUID,BLOCK)[0]
    out.pop('coverage_method');out['coverage'].pop('basis');out['coverage'].pop('stake_pool_total_rao')
    contract=retained(tmp_path,out)
    assert retained_coverage_method(tmp_path,contract)==COVERAGE_LEGACY
    from noah.reports.artifacts import check_retained_engine
    assert check_retained_engine(tmp_path,contract,_compute(c,NETUID,BLOCK)[0])[1]=='retained_engine_mismatch'


def test_gated_outcomes_are_explicit_and_old_missing_engine_is_only_legacy(tmp_path):
    new=compute(chain(complete=None),NETUID,BLOCK)[0]
    contract=retained(tmp_path,new)
    assert retained_coverage_method(tmp_path,contract)==COVERAGE_STAKED_V2
    empty=tmp_path/'empty';empty.mkdir()
    assert retained_coverage_method(empty,contract)==COVERAGE_LEGACY
    (empty/'engine-binding.json').write_bytes(b'{}\n')
    with pytest.raises(OSError):retained_coverage_method(empty,contract)


@pytest.mark.parametrize('kind',['fifo','symlink','hardlink','oversized','array'])
def test_retained_input_file_admission_never_blocks_or_accepts_wrong_shape(tmp_path,kind):
    import os
    contract=retained(tmp_path,compute(chain(),NETUID,BLOCK)[0])
    path=tmp_path/'engine.json';raw=path.read_bytes();path.unlink()
    if kind=='fifo':
        import subprocess, sys
        os.mkfifo(path)
        code = """import json,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from noah.reports.artifacts import retained_coverage_method
try:
    retained_coverage_method(Path(sys.argv[2]),json.loads(sys.argv[3]))
except (ValueError,OSError):
    print('REFUSED')
else:
    raise AssertionError('FIFO accepted')
"""
        done = subprocess.run([sys.executable,'-I','-B','-c',code,
            str(Path(__file__).resolve().parents[3]),str(tmp_path),json.dumps(contract)],
            capture_output=True,text=True,timeout=5,check=True)
        assert done.stdout=='REFUSED\n'
        return
    elif kind=='symlink':
        other=tmp_path/'other';other.write_bytes(raw);path.symlink_to(other)
    elif kind=='hardlink':
        other=tmp_path/'other';other.write_bytes(raw);os.link(other,path)
    elif kind=='oversized':path.write_bytes(b' '*((2<<20)+1))
    else:path.write_bytes(b'[]\n')
    with pytest.raises((ValueError,OSError)):retained_coverage_method(tmp_path,contract)


def test_fresh_package_boundary_rejects_legacy_even_when_historical_shape_valid(tmp_path):
    from noah.reports.adjudication import _semantic_reconstruction
    from noah.reports.common import digest
    contract=fixture()['round']
    engine=_compute(chain(),NETUID,BLOCK)[0]
    contract.update(block_hash=BLOCK[2:],block_number=engine['block_number'],reported_netuid=NETUID,
                    ark_coldkey=engine['ark_coldkey'][2:])
    for label in ('reader_a','reader_b'):
        retained(tmp_path/'rounds'/digest(contract)/label/'collection',engine)
    with pytest.raises(ValueError,match='fresh collection'):
        _semantic_reconstruction({'policy':{'required_rounds':[contract]}},tmp_path,fresh=True)


def test_mixed_reader_or_round_coverage_is_not_a_valid_package(tmp_path):
    from noah.reports.adjudication import _semantic_reconstruction
    from noah.reports.common import digest
    new=compute(chain(),NETUID,BLOCK)[0];old=_compute(chain(),NETUID,BLOCK)[0]
    contract=fixture()['round'];contract.update(block_hash=BLOCK[2:],block_number=new['block_number'],
        reported_netuid=NETUID,ark_coldkey=new['ark_coldkey'][2:])
    for label,value in [('reader_a',new),('reader_b',old)]:
        retained(tmp_path/'rounds'/digest(contract)/label/'collection',value)
    with pytest.raises(ValueError,match='mixed coverage'):
        _semantic_reconstruction({'policy':{'required_rounds':[contract]}},tmp_path)
