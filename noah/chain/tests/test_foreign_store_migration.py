"""Atomic schema migration and receipt reconciliation, without live chain writes."""
import sqlite3

import pytest

from noah.chain.store import Store, SchemaTooOld, DayNotScanned, ObservationChanged, NotANoahStore
from noah.chain.runner import run_day
from noah.chain.tests import test_runner as TR

ANCHOR = TR.ANCHOR
FIRST = 8_800_000


def old_store(path, *, foreign=True, native=True):
    store = Store.create(path, day_anchor_ts=ANCHOR)
    if native:
        store.record_noah_deposit(coldkey=TR.GIVER, block_timestamp=ANCHOR+12,
            amount_rao=10, block=FIRST+1, event_ordinal=1, extrinsic_index=0,
            block_hash=f'0x{FIRST+1:x}', carried_by='StakeTransferred')
    store.mark_day_scanned(0, FIRST, FIRST+9)
    store.mark_day_scanned(1, FIRST+10, FIRST+19)  # an empty scan must be invalidated too
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE foreign_deposit')
        conn.execute('''CREATE TABLE foreign_deposit (
            block INTEGER NOT NULL CHECK(block>0), event_ordinal INTEGER NOT NULL CHECK(event_ordinal>=0),
            extrinsic_index INTEGER NOT NULL CHECK(extrinsic_index>=0), coldkey TEXT NOT NULL,
            day INTEGER NOT NULL CHECK(day>=0), amount_rao INTEGER NOT NULL CHECK(amount_rao>=0 AND typeof(amount_rao)='integer'),
            token_netuid INTEGER NOT NULL CHECK(token_netuid>=0), block_hash TEXT NOT NULL,
            PRIMARY KEY(block,event_ordinal))''')
        conn.execute('CREATE INDEX foreign_deposit_by_wallet_day ON foreign_deposit(coldkey,day)')
        if foreign:
            conn.execute('INSERT INTO foreign_deposit VALUES (?,?,?,?,?,?,?,?)',
                         (FIRST+2,2,1,TR.GIVER,0,20,99,f'0x{FIRST+2:x}'))
        conn.execute('DROP TABLE day_needs_rescan')
        conn.execute('PRAGMA user_version=17')
    return path


def fingerprint(path):
    with sqlite3.connect(path) as conn:
        schema = list(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
        rows = {name: list(conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'))
                for kind,name,_ in schema if kind == 'table'}
        return (conn.execute('PRAGMA user_version').fetchone()[0], schema, rows)


def test_open_migrates_once_preserves_native_sql_rows_and_marks_even_empty_scans(tmp_path):
    path = old_store(tmp_path/'old.db')
    before = fingerprint(path)
    store = Store.open(path, day_anchor_ts=ANCHOR)
    assert store.pending_foreign_rescans() == [(0,FIRST,FIRST+9),(1,FIRST+10,FIRST+19)]
    assert not store.day_was_scanned(0) and not store.day_was_scanned(1)
    assert store._conn.execute('SELECT hotkey,owner_at_deposit,origin_hotkey,unattributed_reason,block_timestamp FROM foreign_deposit').fetchone() == (None,)*5
    store.close()
    after = fingerprint(path)
    for name in before[2]:
        if name != 'foreign_deposit':
            assert after[2][name] == before[2][name]
            assert next(r for r in after[1] if r[1] == name) == next(r for r in before[1] if r[1] == name)
    first_bytes = path.read_bytes()
    Store.open(path, day_anchor_ts=ANCHOR).close()
    Store.create(path, day_anchor_ts=ANCHOR).close()
    assert path.read_bytes() == first_bytes


@pytest.mark.parametrize('version',[0,16,19,999])
def test_unknown_schema_refused_by_both_entry_points_without_changes(tmp_path,version):
    path = old_store(tmp_path/'old.db')
    with sqlite3.connect(path) as conn:
        conn.execute(f'PRAGMA user_version={version}')
    before = path.read_bytes()
    for entry in (Store.open,Store.create):
        with pytest.raises(SchemaTooOld): entry(path,day_anchor_ts=ANCHOR)
        assert path.read_bytes() == before


@pytest.mark.parametrize('partial',["ALTER TABLE foreign_deposit ADD COLUMN hotkey TEXT", "CREATE TABLE day_needs_rescan(day INTEGER PRIMARY KEY)"])
def test_partial17_refused_without_repair_or_version_stamp(tmp_path,partial):
    path = old_store(tmp_path/'partial.db')
    with sqlite3.connect(path) as conn: conn.execute(partial)
    before = path.read_bytes()
    with pytest.raises(SchemaTooOld): Store.open(path,day_anchor_ts=ANCHOR)
    assert path.read_bytes() == before


def test_failure_after_first_ddl_rolls_back_every_schema_change(tmp_path,monkeypatch):
    path = old_store(tmp_path/'old.db')
    before = fingerprint(path)
    real_connect = sqlite3.connect
    def connect(*args,**kwargs):
        conn = real_connect(*args,**kwargs)
        if str(args[0]) == str(path):
            def auth(action,first,second,*rest):
                if action == sqlite3.SQLITE_ALTER_TABLE and second == 'foreign_deposit':
                    auth.calls += 1
                    if auth.calls == 2: return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            auth.calls=0
            conn.set_authorizer(auth)
        return conn
    monkeypatch.setattr(sqlite3,'connect',connect)
    with pytest.raises(NotANoahStore): Store.open(path,day_anchor_ts=ANCHOR)
    monkeypatch.setattr(sqlite3,'connect',real_connect)
    assert fingerprint(path) == before
    Store.open(path,day_anchor_ts=ANCHOR).close()
    assert fingerprint(path)[0] == 18


def test_legacy_foreign_only_enriched_by_exact_reobserved_receipt_and_hash_forwarded(tmp_path):
    path = old_store(tmp_path/'old.db',native=False)
    store = Store.open(path,day_anchor_ts=ANCHOR)
    kwargs=dict(coldkey=TR.GIVER,block_timestamp=ANCHOR+24,amount_rao=20,token_netuid=99,
                block=FIRST+2,event_ordinal=2,extrinsic_index=1,block_hash=f'0x{FIRST+2:x}',
                hotkey=TR.HOTKEY,owner_at_deposit=TR.GIVER,origin_hotkey='named-miner')
    with pytest.raises(ObservationChanged): store.record_foreign_deposit(**{**kwargs,'amount_rao':21})
    store.record_foreign_deposit(**kwargs)
    assert store._conn.execute('SELECT block_timestamp FROM foreign_deposit').fetchone()[0] == ANCHOR+24
    with pytest.raises(ObservationChanged): store.record_foreign_deposit(**{**kwargs,'block_timestamp':ANCHOR+25})
    with pytest.raises(DayNotScanned): store.mark_day_scanned(0,FIRST,FIRST+9)
    with pytest.raises(DayNotScanned): store.mark_day_scanned(0,FIRST+1,FIRST+9,observed_receipts={(FIRST+2,2)})
    with pytest.raises(ObservationChanged): store.mark_day_scanned(0,FIRST,FIRST+9,observed_receipts=set())
    store.mark_day_scanned(0,FIRST,FIRST+9,observed_receipts={(FIRST+2,2)})
    calls=[]
    def rule(*args): calls.append(args); return 7
    assert store.foreign_deposit_equivalent_for_hotkey(TR.GIVER,'named-miner',0,rule) == 7
    assert calls == [(20,99,FIRST+2,f'0x{FIRST+2:x}')]
    store.close()


def test_runner_cannot_clear_stale_day_when_chain_omits_old_native_receipt(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False)
    store=Store.open(path,day_anchor_ts=ANCHOR)
    with pytest.raises(ObservationChanged):
        run_day(store,TR.Chain(),day=0,from_block=FIRST,to_block=FIRST+9,
                ark_coldkey=TR.ARK,designated_validator_hotkey=TR.HOTKEY,
                sources={'a':'wss://a:443','b':'wss://b:443'},netuid=TR.NETUID,
                holding_stride=100,read_collateral=True)
    assert not store.day_was_scanned(0)
    assert store._conn.execute('SELECT count(*) FROM noah_deposit').fetchone()[0] == 1
    store.close()


def test_runner_full_empty_original_range_can_be_proven_empty(tmp_path):
    path=old_store(tmp_path/'old.db',foreign=False,native=False)
    store=Store.open(path,day_anchor_ts=ANCHOR)
    run_day(store,TR.Chain(),day=0,from_block=FIRST,to_block=FIRST+9,
            ark_coldkey=TR.ARK,designated_validator_hotkey=TR.HOTKEY,
            sources={'a':'wss://a:443','b':'wss://b:443'},netuid=TR.NETUID,
            holding_stride=100,read_collateral=True)
    assert store.day_was_scanned(0) and not store.day_was_scanned(1)
    store.close()


def test_wrong_anchor_cannot_start_migration(tmp_path):
    from noah.chain.store import AnchorMismatch
    path = old_store(tmp_path/'old.db')
    before = path.read_bytes()
    for entry in (Store.create, Store.open):
        with pytest.raises(AnchorMismatch): entry(path, day_anchor_ts=ANCHOR+86400)
        assert path.read_bytes() == before


def test_identical_native_receipt_reobservation_is_byte_identical(tmp_path):
    path = old_store(tmp_path/'old.db',foreign=False)
    store = Store.open(path,day_anchor_ts=ANCHOR)
    before = path.read_bytes()
    store.record_noah_deposit(coldkey=TR.GIVER,block_timestamp=ANCHOR+12,
        amount_rao=10,block=FIRST+1,event_ordinal=1,extrinsic_index=0,
        block_hash=f'0x{FIRST+1:x}',carried_by='StakeTransferred')
    assert path.read_bytes() == before
    assert not store.day_was_scanned(0)
    store.close()


def test_partial_original_range_refused_before_chain_read_or_receipt_write(tmp_path):
    path = old_store(tmp_path/'old.db')
    store = Store.open(path,day_anchor_ts=ANCHOR)
    before = path.read_bytes()
    chain = TR.Chain()
    with pytest.raises(DayNotScanned):
        run_day(store,chain,day=0,from_block=FIRST+1,to_block=FIRST+9,
                ark_coldkey=TR.ARK,designated_validator_hotkey=TR.HOTKEY,
                sources={'a':'wss://a:443','b':'wss://b:443'},netuid=TR.NETUID,
                holding_stride=100,read_collateral=True)
    assert chain.asked == [] and path.read_bytes() == before
    store.close()
