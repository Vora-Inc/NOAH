"""Recovery proves original receipts without rewriting unrelated history."""
import copy
import importlib.util
import io
from pathlib import Path
import pytest
from noah.chain.agreeing import AgreeingChain
from noah.chain.foreign_rescan import ForeignRescanRefused, plan_foreign_rescans, rescan_foreign_receipts
from noah.chain.sources import Refusal
from noah.chain.store import Store, DayNotScanned, ObservationChanged
from noah.chain.tests import test_runner as TR
from noah.chain.tests.test_foreign_store_migration import old_store, fingerprint, FIRST, ANCHOR


def readers(deposits=None, extrinsics=None, owners=None, right=None):
    kwargs=dict(deposits=deposits or {},extrinsics=extrinsics or {},owners_at_hash=owners)
    left=TR.Chain(**kwargs)
    right=right or TR.Chain(**kwargs)
    for source in (left,right):
        source.published_ark_pin=lambda block:TR.ARK
    chain=AgreeingChain({'author':left,'reader':right},
        {'author':'wss://author.example','reader':'wss://reader.example'},canary=lambda c,b:256)
    return chain,left,right


def replay(store,chain,**overrides):
    args=dict(day=0,from_block=FIRST,to_block=FIRST+9,ark_coldkey=TR.ARK,
        designated_validator_hotkey=TR.HOTKEY,netuid=TR.NETUID,floor_block=FIRST)
    args.update(overrides)
    return rescan_foreign_receipts(store,chain,**args)


def test_plan17_is_byte_identical_and_includes_empty_old_ranges(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    before=path.read_bytes()
    assert plan_foreign_rescans(path)==[(0,FIRST,FIRST+9),(1,FIRST+10,FIRST+19)]
    assert path.read_bytes()==before


def test_empty_full_range_proof_and_completed_day_noop(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    assert replay(store,chain)==0
    assert store.day_was_scanned(0) and not store.day_was_scanned(1)
    for reader in (left,right):
        for kind in ('events','time','extrinsics'):
            assert {b for k,b in reader.asked if k==kind}==set(range(FIRST,FIRST+10))
    before=path.read_bytes();left.asked.clear();right.asked.clear()
    assert replay(store,chain)==0
    assert not left.asked and not right.asked and path.read_bytes()==before
    store.close()


def test_floor_prefix_read_but_cannot_create_receipt(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers(deposits={FIRST+1:TR.a_deposit()})
    assert replay(store,chain,floor_block=FIRST+3)==0
    assert store._conn.execute('SELECT count(*) FROM noah_deposit').fetchone()[0]==0
    for reader in (left,right):
        assert ('events',FIRST+1) in reader.asked and ('time',FIRST+1) in reader.asked
        assert ('extrinsics',FIRST+1) not in reader.asked
    store.close()


def test_existing_receipt_below_floor_refused_before_reads_or_writes(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    before=path.read_bytes()
    with pytest.raises(ForeignRescanRefused,match='below subnet floor'):
        replay(store,chain,floor_block=FIRST+3)
    assert not left.asked and not right.asked and path.read_bytes()==before
    store.close()


def test_missing_old_native_receipt_cannot_clear_marker(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    with pytest.raises(ObservationChanged,match='complete recorded receipt'):
        replay(store,readers()[0])
    assert not store.day_was_scanned(0)
    assert store._conn.execute('SELECT count(*) FROM noah_deposit').fetchone()[0]==1
    store.close()


def test_partial_range_rejected_before_chain_reads(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    with pytest.raises(DayNotScanned): replay(store,chain,from_block=FIRST+1)
    assert not left.asked and not right.asked
    store.close()


def test_second_reader_failure_keeps_marker_and_unrelated_tables(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    before=fingerprint(path)
    chain,_,_=readers(right=TR.Chain(fail_at=FIRST+2))
    with pytest.raises(Refusal): replay(store,chain)
    assert fingerprint(path)==before and not store.day_was_scanned(0)
    store.close()


def test_timestamp_disagreement_keeps_marker(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    right=TR.Chain();right.block_timestamp=lambda b:ANCHOR+(b-FIRST)*12+1
    with pytest.raises(Refusal):replay(store,readers(right=right)[0])
    assert not store.day_was_scanned(0)
    store.close()


def test_foreign_recovery_stores_exact_time_owner_and_preserves_all_other_tables(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    TR._held(store,TR.GIVER)
    before=fingerprint(path)
    block=FIRST+2
    events=copy.deepcopy(TR._a_batch_all_deposit())
    events[0]['attributes'][4]=99
    events[1]['attributes'][3:5]=[99,99]
    extrinsics=copy.deepcopy(TR._batch_all_extrinsics())
    args=extrinsics[7]['call']['call_args'][0]['value'][0]['call_args']
    for arg in args:
        if arg['name'] in ('origin_netuid','destination_netuid'):arg['value']=99
    chain,left,right=readers(deposits={block:events},extrinsics={block:extrinsics},
        owners={(TR._MINER_HOTKEY,f'0x{block-1:x}'):TR.GIVER})
    assert replay(store,chain)==1
    row=store._conn.execute('SELECT block_timestamp,token_netuid,hotkey,origin_hotkey,owner_at_deposit,unattributed_reason FROM foreign_deposit').fetchone()
    assert row==(ANCHOR+24,99,TR.HOTKEY,TR._MINER_HOTKEY,TR.GIVER,None)
    after=fingerprint(path)
    for name,rows in before[2].items():
        if name not in ('foreign_deposit','day_needs_rescan'):
            assert after[2][name]==rows
    assert after[1]==before[1]
    for source in (left,right):
        assert ('owner_at',TR._MINER_HOTKEY,f'0x{block-1:x}') in source.asked
    store.close()


def test_single_source_refused(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    with pytest.raises(ForeignRescanRefused,match='independent'):replay(store,TR.Chain())
    store.close()


def test_cli_default_is_offline_readonly17_plan(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    cli_path=Path(__file__).resolve().parents[3]/'scripts'/'rescan_foreign_receipts.py'
    spec=importlib.util.spec_from_file_location('rescan_cli_test',cli_path)
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    before=path.read_bytes();out=io.StringIO()
    assert cli.main(['--history-db',str(path),'--max-days','25'],out=out)==0
    assert '"pending_days": 2' in out.getvalue() and path.read_bytes()==before


def test_registration_floor_checked_from_both_readers_at_one_finalized_hash():
    from noah.chain.foreign_rescan import verify_registration_floor
    class Node:
        def __init__(self,value):self.value=value;self.asked=[]
        def query(self,module,item,params,block_hash):
            self.asked.append((module,item,params,block_hash));return self.value
    chain,left,right=readers()
    for source in (left,right):
        source._connection=Node(FIRST)
        source._ask=lambda label,question:question()
    verify_registration_floor(chain,netuid=35,floor_block=FIRST)
    assert left._connection.asked==right._connection.asked
    assert left._connection.asked==[('SubtensorModule','NetworkRegisteredAt',[35],f'0x{left.head:x}')]
    right._connection.value=FIRST+1
    with pytest.raises(ForeignRescanRefused,match='configured floor'):
        verify_registration_floor(chain,netuid=35,floor_block=FIRST)


def test_block_hash_change_keeps_marker_even_when_block_is_empty(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    original=chain.block_hash
    calls={}
    def changed(block):
        calls[block]=calls.get(block,0)+1
        return '0xchanged' if block==FIRST and calls[block]>1 else original(block)
    chain.block_hash=changed
    with pytest.raises(ForeignRescanRefused,match='changed'):replay(store,chain)
    assert not store.day_was_scanned(0)
    store.close()


def test_wrong_ark_cannot_clear_even_an_empty_day(tmp_path):
    from noah.core.chain_adapter import ArkIdentityMismatch
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    before=path.read_bytes()
    with pytest.raises(ArkIdentityMismatch):replay(store,chain,ark_coldkey=TR.GIVER)
    assert path.read_bytes()==before and not store.day_was_scanned(0)
    assert not any(item[0]=='events' for item in left.asked+right.asked)
    store.close()


def test_ark_identity_checks_only_post_registration_boundaries(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    observed=[]
    for source in (left,right):
        source.published_ark_pin=lambda block:observed.append(block) or TR.ARK
    replay(store,chain,floor_block=FIRST+3)
    assert observed==[FIRST+3,FIRST+3,FIRST+9,FIRST+9,FIRST+9,FIRST+9]
    store.close()


def test_cli_held_publisher_lock_refuses_before_migration_or_rpc(tmp_path,monkeypatch):
    import fcntl
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    cli_path=Path(__file__).resolve().parents[3]/'scripts'/'rescan_foreign_receipts.py'
    spec=importlib.util.spec_from_file_location('rescan_cli_locked',cli_path)
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    monkeypatch.setattr(cli,'build_dual_sourced_day_chain',lambda **kw:pytest.fail('must not connect'))
    before=path.read_bytes();out=io.StringIO();lock=tmp_path/'publisher.lock'
    with lock.open('a+b') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=cli.main(['--history-db',str(path),'--apply','--netuid','35',
            '--floor-block',str(FIRST),'--ark-address',TR.ARK,'--validator-hotkey',TR.HOTKEY,
            '--expected-genesis','0xgenesis','--publisher-lock',str(lock)],out=out)
    assert result==1 and 'publisher lock is held' in out.getvalue()
    assert path.read_bytes()==before


def test_historical_ark_pin_disagreement_preserves_empty_marker(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.create(path,day_anchor_ts=ANCHOR)
    chain,left,right=readers()
    right.published_ark_pin=lambda block:TR.GIVER
    before=path.read_bytes()
    with pytest.raises(Refusal):replay(store,chain)
    assert path.read_bytes()==before and not store.day_was_scanned(0)
    store.close()


def test_public_reader_builder_uses_distinct_connections_and_closes_on_failure(monkeypatch):
    from types import SimpleNamespace
    from noah.chain import foreign_rescan

    connections = []
    def connect(variable):
        connection = SimpleNamespace(closed=False)
        connection.close = lambda: setattr(connection, 'closed', True)
        connections.append(connection)
        return SimpleNamespace(_connection=connection, endpoint=f'wss://{variable.lower()}.example')
    monkeypatch.setattr(foreign_rescan, 'live_chain_source_from_environment', connect)
    chain = foreign_rescan.build_dual_sourced_day_chain(netuid=2)
    assert len(connections) == 2
    assert chain._sources['author']._connection is not chain._sources['reader']._connection
    assert not any(c.closed for c in connections)
    for source in chain._sources.values():
        source._connection.close()

    connections.clear()
    def fail_second(variable):
        if connections:
            raise Refusal('reader unavailable')
        return connect(variable)
    monkeypatch.setattr(foreign_rescan, 'live_chain_source_from_environment', fail_second)
    with pytest.raises(Refusal, match='reader unavailable'):
        foreign_rescan.build_dual_sourced_day_chain(netuid=2)
    assert len(connections) == 1 and connections[0].closed


def test_cli_inherited_lock_keeps_parent_ownership_and_rejects_other_inode(tmp_path,monkeypatch):
    import fcntl
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    cli_path=Path(__file__).resolve().parents[3]/'scripts'/'rescan_foreign_receipts.py'
    spec=importlib.util.spec_from_file_location('rescan_cli_inherited',cli_path)
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    lock=tmp_path/'publisher.lock';other=tmp_path/'other.lock';other.touch()
    before=path.read_bytes();called=[]
    def connection(**kw):
        called.append(True)
        with lock.open('a+b') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        raise ValueError('stopped before migration')
    monkeypatch.setattr(cli,'build_dual_sourced_day_chain',connection)
    with lock.open('a+b') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        base=['--history-db',str(path),'--apply','--netuid','35','--floor-block',str(FIRST),
              '--ark-address',TR.ARK,'--validator-hotkey',TR.HOTKEY,'--expected-genesis','0xgenesis',
              '--publisher-lock-fd',str(owner.fileno())]
        out=io.StringIO()
        assert cli.main(base+['--publisher-lock',str(other)],out=out)==1
        assert 'does not match' in out.getvalue() and not called
        out=io.StringIO()
        assert cli.main(base+['--publisher-lock',str(lock)],out=out)==1
        assert called and 'stopped before migration' in out.getvalue()
        with lock.open('a+b') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert path.read_bytes()==before
