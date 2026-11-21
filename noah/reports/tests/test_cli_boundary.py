import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from noah.reports import cli, dispatch, validator
from noah.reports.backing import collector
from noah.reports.backing.bootstrap import extend_history
from noah.reports.backing.offline import _history_inputs
from noah.reports.tests.test_history import SyntheticChain, _round


def test_dispatch_isolated_without_shell_or_inherited_import_path(monkeypatch):
    monkeypatch.setenv('NOAH_REPORT_PYTHON',sys.executable)
    monkeypatch.setenv('PYTHONPATH','wrong-checkout')
    seen={}
    def run(command,**kw):seen.update(command=command,**kw);return 7
    monkeypatch.setattr(dispatch.subprocess,'call',run)
    assert dispatch.dispatch('miner',['status','--round','with spaces.json'])==7
    assert seen['command']==[sys.executable,'-I','-m','noah.reports.cli','miner','status','--round','with spaces.json']
    assert 'PYTHONPATH' not in seen['env'] and 'shell' not in seen


def test_both_primary_help_commands_work_without_validator_sdk(capsys):
    assert dispatch.validator_main(['--help'])==0
    assert dispatch.validator_main(['check','--help'])==0
    assert 'reports' in capsys.readouterr().out
    from noah.deposits.assistant.miner_cli import main
    with pytest.raises(SystemExit) as result: main(['--help'])
    assert result.value.code==0
    assert 'report' in capsys.readouterr().out


def test_miner_report_delegation_preserves_config(monkeypatch):
    from noah.deposits.assistant.miner_cli import main
    seen=[]
    monkeypatch.setattr(dispatch,'dispatch',lambda *a: seen.append(a) or 0)
    assert main(['--config','miner.ini','report','status','--round','r.json'])==0
    assert seen==[('miner',['--config','miner.ini','status','--round','r.json'])]


def test_optional_gate_parser_defaults_off():
    p=cli.parser()
    assert not p.parse_args(['validator','score-preview','--fixture','x']).apply_reports
    assert p.parse_args(['validator','score-preview','--fixture','x','--require-accurate-miner-report']).apply_reports


def test_refusal_does_not_leak_endpoint_credentials(monkeypatch,capsys):
    from noah.chain.sources import Refusal
    def fail(_): raise Refusal('wss://' + 'user' + ':' + 'secret' + '@node.invalid/path?' + 'token' + '=secret')
    monkeypatch.setattr(cli,'execute',fail)
    assert cli.main(['validator','status','--evidence','x'])==2
    assert 'secret' not in capsys.readouterr().err


def test_collect_requires_deliberate_bootstrap_before_network(tmp_path):
    chain=SyntheticChain(8);r=_round(chain,6)
    with pytest.raises(ValueError,match='history'):
        validator.collect_report(r,['ws://fixture.invalid'],['11'*32],tmp_path)


def test_retained_package_can_move_and_same_head_can_be_collected_twice(tmp_path):
    chain=SyntheticChain(8);r=_round(chain,6);history=tmp_path/'history'
    result=extend_history(chain,history,0,5,ark_coldkey=r['ark_coldkey'],
        ark_validator_hotkey=r['ark_validator_hotkey'],eligibility_policy_version=r['eligibility_policy_version'])
    assert result['advanced']
    checkpoint=json.loads((history/'checkpoint.json').read_text())
    args=(tmp_path/'out',r,{}, {'endpoint':'ws://fixture.invalid'},'0x00',{},[],[],lambda:0,[],['11'*32],checkpoint)
    first=collector._retain(*args,history_dir=history)
    second=collector._retain(*args,history_dir=history)
    assert first!=second
    moved=tmp_path/'moved'; first.rename(moved);history.rename(tmp_path/'old-history')
    doc,root,problem=_history_inputs(moved)
    assert problem is None and root==moved
    assert 'segment_root' not in doc


def test_collector_records_endpoint_label_and_error_type_only(monkeypatch):
    rpc=NS(init_runtime=lambda:None)
    monkeypatch.setattr(collector,'live_sdk',lambda *_args,**_kwargs:rpc)
    monkeypatch.setattr(collector,'_attach_recorder',lambda _, **_kwargs:[])
    _,label=collector._connect(['wss://' + 'user' + ':' + 'secret' + '@node.invalid/path?' + 'token' + '=secret'])
    assert label=='wss://node.invalid'


def test_uncertain_send_search_can_reach_beyond_old_64_block_limit():
    from noah.reports.backing.chain_session import SubstrateChain
    class RPC:
        def get_block_header(self,h):return {'header':{'number':int(h),'parentHash':str(int(h)-1)}}
        def get_block(self,h):return {'extrinsics':[NS(extrinsic_hash='0xab')] if h=='2' else []}
    chain=SubstrateChain.__new__(SubstrateChain);chain.substrate=RPC();chain.search_from_block=1
    chain._rpc=lambda *_:'100'
    chain._inclusion_from_block=lambda *args:args
    assert chain.find('0xab')==('2',0,'0xab')


def test_completed_bootstrap_still_checks_round_identity(tmp_path):
    chain=SyntheticChain(8);r=_round(chain,6);history=tmp_path/'history'
    assert extend_history(chain,history,0,5,ark_coldkey=r['ark_coldkey'],
        ark_validator_hotkey=r['ark_validator_hotkey'],eligibility_policy_version=r['eligibility_policy_version'])['advanced']
    wrong={**r,'genesis_hash':'aa'*32}
    with pytest.raises(ValueError,match='genesis'):
        validator.history_tick(wrong,'ws://unused.invalid',history,5,client=chain)


def test_private_schedule_invocation_and_ambiguous_options(tmp_path, monkeypatch, capsys):
    from noah.reports import schedule
    document = {'schedule': {'fixture': True}, 'day': 3,
                'endpoints': ['ws://reader-a.invalid', 'ws://reader-b.invalid'], 'require_pair': True}
    path = tmp_path / 'private.json'
    path.write_text(json.dumps(document))
    calls = []
    monkeypatch.setattr(schedule, 'resolve_day', lambda *args, **kwargs: calls.append((args, kwargs)) or {'resolved': True})
    assert cli.main(['validator', 'resolve-day', '--invocation', str(path)]) == 0
    assert calls == [((document['schedule'], document['endpoints'], 3), {'require_pair': True})]
    assert cli.main(['validator', 'resolve-day', '--invocation', str(path), '--day', '3']) == 2
    assert len(calls) == 1
    document['require_pair'] = 1
    path.write_text(json.dumps(document))
    assert cli.main(['validator', 'resolve-day', '--invocation', str(path)]) == 2
    assert len(calls) == 1


def test_resolution_replay_disables_network_before_reconstruction(tmp_path, monkeypatch):
    from noah.reports import schedule
    from noah.reports.backing import offline
    path = tmp_path / 'proof.json'
    path.write_text('{}')
    calls = []
    monkeypatch.setattr(offline, 'disable_network', lambda: calls.append('offline'))
    def verify(*args, **kwargs):
        assert calls == ['offline'] and kwargs == {'require_pair': True}
        return {'verified': True}
    monkeypatch.setattr(schedule, 'verify_resolution', verify)
    assert cli.main(['validator', 'verify-resolution', '--schedule', str(path),
                     '--day', '3', '--resolution', str(path)]) == 0


def test_scheduled_cli_defaults_to_unsigned_single_tick(tmp_path, monkeypatch):
    from noah.deposits.assistant import config
    from noah.reports import schedule_runner
    path = tmp_path / 'schedule.json'
    path.write_text('{}')
    monkeypatch.setattr(config, 'load_config', lambda _: 'configuration')
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
    calls = []
    monkeypatch.setattr(schedule_runner, 'scheduled_tick', lambda *args, **kwargs: calls.append((args, kwargs)) or {'sent': False})
    assert cli.main(['miner', 'run', '--schedule', str(path)]) == 0
    assert calls == [(('configuration', {}), {'authorized': False, 'allow_mainnet': False})]


def test_public_export_command_requires_all_pins_and_dispatches_without_upload(monkeypatch, capsys):
    from noah.reports import public_export_cli
    flags = ['--request', 'private.json', '--package', 'proof', '--manifest-sha256', 'aa'*32,
             '--round-sha256', 'bb'*32, '--activation-receipt', 'approval.json',
             '--activation-receipt-sha256', 'cc'*32, '--published-at', '2026-11-16T00:00:00Z',
             '--out', 'public', '--receipt', 'private-receipt.json']
    called = []
    def execute(args):
        called.append(args)
        return {'status': 'PREPARED', 'publication_performed': False}
    monkeypatch.setattr(public_export_cli, 'execute', execute)
    assert cli.main(['validator', 'export-public', *flags]) == 0
    assert len(called) == 1 and called[0].manifest_sha256 == 'aa'*32
    assert json.loads(capsys.readouterr().out)['publication_performed'] is False
    with pytest.raises(SystemExit) as refusal:
        cli.parser().parse_args(['validator', 'export-public', *flags[:-2]])
    assert refusal.value.code == 2
    assert len(called) == 1


@pytest.mark.parametrize('action, expected', [('complete', 0), ('expired', 1)])
def test_scheduled_follower_stops_at_terminal_round_without_timeout(tmp_path, monkeypatch, action, expected):
    from noah.deposits.assistant import config
    from noah.reports import schedule_runner
    path = tmp_path / 'schedule.json'
    path.write_text('{}')
    monkeypatch.setattr(config, 'load_config', lambda _: 'configuration')
    calls = []
    monkeypatch.setattr(schedule_runner, 'scheduled_tick', lambda *_args, **_kwargs: calls.append(action) or {'action': action, 'sent': False})
    monkeypatch.setattr(cli.time, 'sleep', lambda *_: pytest.fail('terminal round must not poll again'))
    assert cli.main(['miner', 'run', '--schedule', str(path), '--yes', '--follow']) == expected
    assert calls == [action]


@pytest.mark.parametrize('command', ['submit', 'run'])
@pytest.mark.parametrize('tick_duration', [0, 11])
def test_miner_follower_does_not_start_next_phase_at_timeout(tmp_path, monkeypatch, command, tick_duration):
    from noah.deposits.assistant import config
    from noah.reports import miner, schedule_runner
    path = tmp_path / 'round-or-schedule.json'
    path.write_text('{}')
    monkeypatch.setattr(config, 'load_config', lambda _: 'configuration')
    monkeypatch.setattr(cli, 'load_round', lambda _: {})
    clock = [0]
    calls = []
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def tick(*args, **kwargs):
        calls.append(kwargs)
        assert len(calls) == 1, 'must not start a reveal tick at or after the deadline'
        clock[0] += tick_duration
        return {'action': 'commit', 'sent': True}

    monkeypatch.setattr(miner, 'submit_tick', tick)
    monkeypatch.setattr(schedule_runner, 'scheduled_tick', tick)
    option = '--round' if command == 'submit' else '--schedule'
    assert cli.main(['miner', command, option, str(path), '--yes', '--follow',
                     '--timeout-seconds', '10']) == 1
    assert calls == [{'authorized': True, 'allow_mainnet': False}]
    assert clock[0] >= 10


def test_relative_worker_executes_checked_file_not_path_search(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked = tmp_path / 'report-python'
    checked.write_text('selected fixture'); checked.chmod(0o700)
    monkeypatch.setenv('NOAH_REPORT_PYTHON', checked.name)
    monkeypatch.setenv('PATH', str(tmp_path / 'different-executable-directory'))
    calls = []
    monkeypatch.setattr(dispatch.subprocess, 'call', lambda command, **kw: calls.append(command) or 0)
    assert dispatch.dispatch('miner', ['status']) == 0
    assert calls[0][0] == str(checked)
