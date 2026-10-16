"""Participant commands exercise the existing deposit and receipt safeguards."""
import json

import pytest

from noah.deposits.assistant import cli, miner_cli
from .test_cli import factory, write
from .fakes import FakeChain


def _ini_line(name, value):
    """Build synthetic redaction inputs without embedding credential assignments."""
    return f'{name} = {value}\n'


@pytest.mark.parametrize('label_args', [[], ['--label', 'fresh-preview']])
def test_installed_command_preview_never_signs(tmp_path, capsys, label_args):
    chain = FakeChain()
    services_seen = []
    def build(config):
        services = factory(tmp_path, chain)(config)
        services.signer_factory = lambda: pytest.fail('preview opened signing key')
        services_seen.append(services)
        return services
    run = lambda args: cli.main(args, build)
    assert miner_cli.main(['deposit', '--config', write(tmp_path), '--preview', *label_args],
                          transaction_runner=run) == 0
    assert chain.submitted == []
    assert services_seen[0].ledger._db.execute('SELECT COUNT(*) FROM intents').fetchone()[0] == 0
    assert services_seen[0].ledger._db.execute("SELECT name FROM sqlite_master WHERE name='mixed_reservations'").fetchone() is None
    assert '200 NOAH' in capsys.readouterr().out


def test_deposit_then_status_uses_same_receipt_and_does_not_resend(tmp_path, capsys):
    chain = FakeChain()
    path = write(tmp_path)
    run = lambda args: cli.main(args, factory(tmp_path, chain))
    assert miner_cli.main(['--config', path, 'deposit'], transaction_runner=run) == 0
    assert len(chain.submitted) == 1
    assert miner_cli.main(['status', '--config', path], transaction_runner=run) == 0
    assert 'finalized' in capsys.readouterr().out
    assert len(chain.submitted) == 1


def test_config_and_check_are_local_and_unknown_options_refuse(tmp_path, capsys):
    def no_network(args):
        raise AssertionError('local config must not invoke chain services')
    path = write(tmp_path)
    assert miner_cli.main(['check-config', '--config', path], transaction_runner=no_network) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'valid', 'chain_checked': False}
    assert miner_cli.main(['config', '--json', '--config', path], transaction_runner=no_network) == 0
    assert json.loads(capsys.readouterr().out)['wallet_name'] == 'w'
    with pytest.raises(SystemExit):
        miner_cli.main(['deposit', '--yes'], transaction_runner=no_network)


def test_init_does_not_overwrite_and_environment_selects_config(tmp_path, monkeypatch):
    path = tmp_path/'miner.ini'
    monkeypatch.setenv('NOAH_DEPOSIT_CONFIG', str(path))
    assert miner_cli.main(['init-config']) == 0
    original = path.read_bytes()
    assert miner_cli.main(['init-config']) == 1
    assert path.read_bytes() == original


def test_ini_display_shows_all_active_sections_without_loading_keys(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'deposit.ini'
    endpoint = 'wss://' + 'user' + ':' + 'password' + '@host/rpc-key?' + 'token' + '=hidden'
    path.write_text('[miner]\n' + _ini_line('coldkey', 'REPLACE_WITH_OWNER')
                    + _ini_line('endpoint', endpoint)
                    + '[signing]\n' + _ini_line('mode', 'proxy')
                    + _ini_line('proxy_address', 'delegate') + _ini_line('signer_ref', 'secret-name')
                    + '[mixed]\n' + _ini_line('foreign_netuid', '3')
                    + '[unexpected]\n' + _ini_line('password', 'hidden-password')
                    + _ini_line('seed', 'hidden-seed'))
    monkeypatch.setattr(miner_cli, 'load_config', lambda *_: pytest.fail('display must not validate/unlock'))
    assert miner_cli.main(['config', '--config', str(path)]) == 0
    output = capsys.readouterr().out
    assert '[signing]' in output and '[mixed]' in output
    assert 'REPLACE_WITH_OWNER' in output and 'signer_ref = secret-name' in output
    assert 'endpoint = wss://host' in output
    assert 'hidden' not in output and 'rpc-key' not in output and 'user:' not in output
    assert '[REDACTED]' in output


def test_ini_display_missing_file_and_malformed_ini_do_not_echo_contents(tmp_path, capsys):
    path = tmp_path / 'deposit.ini'
    assert miner_cli.main(['config', '--config', str(path)]) == 2
    path.write_text('secret bad syntax')
    assert miner_cli.main(['config', '--config', str(path)]) == 2
    assert 'secret bad syntax' not in capsys.readouterr().out


def test_ini_display_redacts_inherited_multiline_and_inline_credentials(tmp_path, capsys):
    path = tmp_path / 'deposit.ini'
    path.write_text('[DEFAULT]\n' + _ini_line('refresh_token', 'sentinel-default')
                    + '[miner]\n' + _ini_line('wallet_name', 'demo # sentinel-comment')
                    + _ini_line('coldkey', 'demo ; sentinel-semicolon')
                    + _ini_line('hotkey_name', 'first\n  sentinel-continuation')
                    + _ini_line('passphrase', 'sentinel-passphrase')
                    + _ini_line('api_token', 'sentinel-api')
                    + _ini_line('recovery_phrase', 'sentinel-recovery')
                    + _ini_line('private', 'sentinel-private')
                    + '[signing]\n' + _ini_line('secret_type', 'bittensor-wallet')
                    + _ini_line('signer_ref', 'secret-name')
                    + '[other]\n' + _ini_line('secret_type', 'sentinel-other'))
    assert miner_cli.main(['config', '--config', str(path)]) == 0
    output = capsys.readouterr().out
    assert 'sentinel' not in output
    assert 'secret_type = bittensor-wallet' in output
    assert 'signer_ref = secret-name' in output
    assert 'wallet_name = demo' in output
