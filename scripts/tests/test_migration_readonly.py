"""Inspection must never migrate the original database as a side effect."""
import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest

from noah.chain.store import SchemaTooOld, Store
from noah.chain.tests.test_foreign_store_migration import ANCHOR, old_store


def cli(name):
    path = Path(__file__).resolve().parents[1] / name
    spec = importlib.util.spec_from_file_location(name.removesuffix('.py'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name,args', [
    ('check_history_contiguity.py', []),
    pytest.param('backfill_missing_history.py', ['--dry-run'], marks=pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / 'backfill_missing_history.py').exists(),
        reason='operator backfill tool is intentionally absent from the public snapshot')),
])
def test_readonly_command_refuses_old_schema_without_migrating(tmp_path, name, args):
    path = old_store(tmp_path / 'history.db')
    before = path.read_bytes()
    out = io.StringIO()
    result = cli(name).main(['--history-db', str(path), '--through-day', '1', *args], out=out)
    assert result == 2
    assert 'explicit migration' in out.getvalue()
    assert path.read_bytes() == before


def test_readonly_store_cannot_migrate_or_mutate_current_state(tmp_path):
    path = old_store(tmp_path / 'history.db')
    before = path.read_bytes()
    with pytest.raises(SchemaTooOld, match='explicit migration'):
        Store.open(path, ANCHOR, read_only=True)
    assert path.read_bytes() == before
    Store.open(path, ANCHOR).close()
    migrated = path.read_bytes()
    store = Store.open(path, ANCHOR, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            store._conn.execute('DELETE FROM noah_deposit')
    finally:
        store.close()
    assert path.read_bytes() == migrated
