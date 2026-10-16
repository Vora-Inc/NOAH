"""Real process/socket shutdown with a deliberately lingering SDK-like thread."""
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest


CHILD = r'''
import json, os, runpy, sys, threading, time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from noah.deposits.assistant import cli, config as config_module, miner_cli, signer
from noah.deposits.assistant.tests.fakes import config
from noah.deposits.assistant.tests.test_participant_signer import settings
root=Path(sys.argv[1]); entry=sys.argv[2]; scenario=sys.argv[3]
cfg=replace(config(), signing=dict(settings(), signer_socket=str(root/'signer.sock')))
config_module.load_config=lambda _: cfg
policy=dict(version=1,policy_id='shutdown-test',runtime_code_hash='0x'+'a'*64,
            spec_version=478,max_amount_rao=10,max_total_rao=100,max_fee_rao=10,
            conversion={'enabled':False})
signer.ini_policy=lambda _: policy
def build(_):
    threading.Thread(target=threading.Event().wait,daemon=False).start()
    def close(kind):
        (root/(kind+'.closed')).write_text('closed')
        if scenario=='cleanup-error' and kind=='connection':raise OSError('PRIVATE_PROVIDER_DETAIL')
    return SimpleNamespace(connection=SimpleNamespace(close=lambda:close('connection')),
                           ledger=SimpleNamespace(close=lambda:close('ledger')))
cli.build_services=build
original=signer.Journal
class Journal(original):
    def __init__(self, directory):
        super().__init__(directory)
        self.put({'id':'preserved','status':'finalized','amount_rao':7})
signer.Journal=Journal
def execute(self, request):
    (root/'entered').write_text('entered')
    calls=root/'calls'
    calls.write_text(str(int(calls.read_text())+1 if calls.exists() else 1))
    self.journal.put({'id':'inflight','status':'prepared','amount_rao':3})
    while not (root/'release').exists():time.sleep(.02)
    status='uncertain' if scenario=='active-uncertain' else 'finalized'
    self.journal.put({'id':'inflight','status':status,'amount_rao':3})
    return {'status':status}
signer.Signer.execute=execute
args=['--config','unused.ini','--journal',str(root/'journal')]
if entry=='participant':
    sys.argv=['noah-miner','signer',*args];miner_cli.entry()
else:
    sys.argv=['signer',*args]
    # The direct module has a separate globals dict; patch the service factory
    # and policy/config inputs through its normal imports and real INI file.
    ini=root/'direct.ini'
    ini.write_text('[signer]\npolicy_id=shutdown-test\nruntime_code_hash=0x'+'a'*64+
       '\nspec_version=478\nmax_amount_rao=10\nmax_total_rao=100\nmax_fee_rao=10\n')
    sys.argv=['signer','--config',str(ini),'--journal',str(root/'journal')]
    runpy.run_module('noah.deposits.assistant.signer',run_name='__main__')
'''


def wait_for(predicate, process, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(process.communicate())
        time.sleep(.02)
    raise AssertionError('child did not reach the expected boundary')


def assert_idle_shutdown(entry, sig, child=CHILD):
    with tempfile.TemporaryDirectory(prefix='noah-sign-', dir='/tmp') as directory:
        root = Path(directory)
        process = subprocess.Popen([sys.executable, '-c', child, directory, entry, 'idle'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_for(lambda: (root/'signer.sock').exists(), process)
            process.send_signal(sig)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, (stdout, stderr)
            assert not (root/'signer.sock').exists()
            assert (root/'connection.closed').exists()
            assert (root/'ledger.closed').exists()
            import sqlite3
            with sqlite3.connect(root/'journal/signer.sqlite') as db:
                assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM intents')]
            if entry != 'direct':
                assert rows == [{'id':'preserved','status':'finalized','amount_rao':7}]
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize('entry', ['participant', 'direct'])
@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_idle_signer_exits_despite_lingering_sdk_thread(entry, sig):
    assert_idle_shutdown(entry, sig)


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('outcome', ['finalized', 'uncertain'])
def test_shutdown_drains_current_request_and_does_not_start_another(sig, outcome):
    with tempfile.TemporaryDirectory(prefix='noah-sign-', dir='/tmp') as directory:
        root = Path(directory)
        scenario = 'active-uncertain' if outcome == 'uncertain' else 'active'
        process = subprocess.Popen([sys.executable, '-c', CHILD, directory, 'participant', scenario],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_for(lambda: (root/'signer.sock').exists(), process)
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(root/'signer.sock'));client.sendall(b'{}\n')
                wait_for(lambda: (root/'entered').exists(), process)
                process.send_signal(sig);process.send_signal(sig)
                time.sleep(.1)
                assert process.poll() is None
                with socket.socket(socket.AF_UNIX) as queued:
                    queued.connect(str(root/'signer.sock'));queued.sendall(b'{}\n')
                    (root/'release').write_text('finish the existing request')
                    assert json.loads(client.recv(1024)) == {'status':outcome}
                    stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, (stdout, stderr)
            assert not (root/'signer.sock').exists()
            import sqlite3
            with sqlite3.connect(root/'journal/signer.sqlite') as db:
                rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM intents ORDER BY id')]
            assert rows == [dict(id='inflight',status=outcome,amount_rao=3),
                            dict(id='preserved',status='finalized',amount_rao=7)]
            assert (root/'calls').read_text() == '1'
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)


@pytest.mark.parametrize('scenario', ['cleanup-error', 'replaced-socket'])
def test_shutdown_reports_cleanup_failure_and_preserves_foreign_socket_path(scenario):
    with tempfile.TemporaryDirectory(prefix='noah-sign-', dir='/tmp') as directory:
        root = Path(directory)
        process = subprocess.Popen([sys.executable, '-c', CHILD, directory, 'participant', scenario],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_for(lambda: (root/'signer.sock').exists(), process)
            if scenario == 'replaced-socket':
                (root/'signer.sock').unlink()
                (root/'signer.sock').write_text('replacement must survive')
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 2, (stdout, stderr)
            assert 'PRIVATE_PROVIDER_DETAIL' not in stdout + stderr
            assert (root/'ledger.closed').exists() and (root/'connection.closed').exists()
            if scenario == 'replaced-socket':
                assert (root/'signer.sock').read_text() == 'replacement must survive'
                assert 'replacement preserved' in stdout
            else:
                assert not (root/'signer.sock').exists()
                assert 'cleanup failed for connection' in stdout
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)


@pytest.mark.parametrize('entry', ['participant', 'direct'])
def test_existing_socket_refusal_closes_resources_without_unlinking(entry):
    with tempfile.TemporaryDirectory(prefix='noah-sign-', dir='/tmp') as directory:
        root = Path(directory)
        (root/'signer.sock').write_text('existing socket placeholder')
        process = subprocess.Popen([sys.executable, '-c', CHILD, directory, entry, 'idle'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 2, (stdout, stderr)
            assert 'socket path already exists' in stdout
            assert (root/'signer.sock').read_text() == 'existing socket placeholder'
            assert (root/'connection.closed').exists() and (root/'ledger.closed').exists()
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)
