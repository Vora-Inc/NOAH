import os
import stat

from noah.deposits.assistant.ledger import Ledger
from noah.deposits.assistant.signer import Journal


def assert_new_private_and_existing_unchanged(path, run):
    previous_umask = os.umask(0o022)
    try:
        run()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.write_bytes(b'preserve existing lock bytes')
        path.chmod(0o644)
        before = path.stat()
        run()
        after = path.stat()
        assert after.st_ino == before.st_ino
        assert path.read_bytes() == b'preserve existing lock bytes'
        assert stat.S_IMODE(after.st_mode) == 0o644
    finally:
        os.umask(previous_umask)


def test_ledger_execution_lock_creation_and_existing_file(tmp_path):
    ledger = Ledger(tmp_path / 'deposits.sqlite')
    path = tmp_path / 'deposits.sqlite.execution.lock'

    def run():
        with ledger.execution_lock():
            pass

    try:
        assert_new_private_and_existing_unchanged(path, run)
    finally:
        ledger.close()


def test_signer_execution_lock_creation_and_existing_file(tmp_path):
    journal = Journal(tmp_path / 'journal')
    path = tmp_path / 'journal/execution.lock'

    def run():
        with journal.locked():
            pass

    try:
        assert_new_private_and_existing_unchanged(path, run)
    finally:
        journal.close()
