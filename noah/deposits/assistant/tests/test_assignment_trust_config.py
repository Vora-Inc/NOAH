"""Weekly report trust is public configuration, independent of the downloaded file."""
from pathlib import Path

import pytest

from noah.deposits.assistant.amounts import DepositRefused
from noah.deposits.assistant.config import load_config
from .test_cli import write


def configured(tmp_path, fields):
    path = Path(write(tmp_path))
    path.write_text(path.read_text() + '\n[report]\n' + fields)
    return path


def test_reporting_disabled_does_not_require_assignment_key(tmp_path):
    config = load_config(write(tmp_path), environ={})
    assert config.assignments_url is None and config.assignment_signer_public_key is None


def test_configured_url_requires_independent_public_key(tmp_path):
    path = configured(tmp_path, 'assignments_url = https://synthetic.invalid/\n')
    with pytest.raises(DepositRefused, match='independently configured'):
        load_config(path, environ={})


@pytest.mark.parametrize('key', ['a' * 63, 'A' * 64, '0x' + 'aa' * 32, 'z' * 64])
def test_malformed_public_key_refuses(tmp_path, key):
    path = configured(tmp_path, 'assignment_signer_public_key = ' + key + '\n')
    with pytest.raises(DepositRefused, match='64 lowercase hex'):
        load_config(path, environ={})


def test_trusted_public_key_is_not_treated_as_secret_material(tmp_path):
    path = configured(tmp_path, 'assignments_url = https://synthetic.invalid/\nassignment_signer_public_key = ' + 'aa' * 32 + '\n')
    config = load_config(path, environ={})
    assert config.assignment_signer_public_key == 'aa' * 32
