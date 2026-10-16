"""Configuration edits are validated locally before atomic replacement."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from noah.chain.sources import Refusal
from noah.deposits.assistant import config_edit as E, miner_cli
from .test_cli import write


def editor(text):
    def run(argv, **kwargs):
        Path(argv[-1]).write_text(text)
        return SimpleNamespace(returncode=0)
    return run


def test_valid_edit_backup_and_no_chain(tmp_path):
    path = Path(write(tmp_path)); original = path.read_text()
    changed = original.replace('reserve_noah = 1', 'reserve_noah = 2')
    assert E.edit_file(path, E.validate_ini, run=editor(changed)) == 0
    assert path.read_text() == changed
    backups = list(tmp_path.glob('*.before-edit-*'))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['invalid', 'editor', 'concurrent', 'symlink'])
def test_failed_edit_never_overwrites(tmp_path, failure):
    path = Path(write(tmp_path)); original = path.read_text()
    def run(argv, **kwargs):
        draft = Path(argv[-1])
        draft.write_text(original.replace('reserve_noah = 1', 'reserve_noah = 3'))
        if failure == 'invalid': draft.write_text('[deposit]\npassword = sentinel\n')
        if failure == 'concurrent': path.write_text(original + '# human edit\n')
        if failure == 'symlink':
            draft.unlink(); draft.symlink_to(path)
        return SimpleNamespace(returncode=1 if failure == 'editor' else 0)
    with pytest.raises(Refusal): E.edit_file(path, E.validate_ini, run=run)
    assert path.read_text() == original + ('# human edit\n' if failure == 'concurrent' else '')
    assert not list(tmp_path.glob('*.before-edit-*'))


def test_readonly_and_shell_not_used(tmp_path, monkeypatch):
    path = Path(write(tmp_path)); path.chmod(0o444)
    with pytest.raises(Refusal): E.edit_file(path, E.validate_ini)
    path.chmod(0o600)
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.setenv('EDITOR', 'editor --arg "literal;$(command)"')
    def run(argv, **kwargs):
        assert argv[:3] == ['editor', '--arg', 'literal;$(command)']
        assert 'shell' not in kwargs
        return SimpleNamespace(returncode=0)
    assert E.edit_file(path, E.validate_ini, run=run) == 0


def test_cli_dispatch_and_json_refusal(tmp_path, monkeypatch):
    path = write(tmp_path)
    monkeypatch.setattr(E, 'edit_ini', lambda p: 0 if p == path else 5)
    assert miner_cli.main(['--config', path, 'config', 'edit']) == 0
    assert miner_cli.main(['--config', path, 'config', 'edit', '--json']) == 2
