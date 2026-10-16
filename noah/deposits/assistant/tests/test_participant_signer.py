"""Public signer setup never unlocks the real owner's wallet."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.chain.ss58 import address_of
from noah.deposits.assistant.amounts import DepositRefused
from noah.deposits.assistant.proxy_signing import validate_signing
from noah.deposits.assistant.signer import ini_policy, load_delegate
from .fakes import config

DELEGATE = address_of(bytes([7])*32)


def settings():
    return dict(mode='proxy', proxy_address=DELEGATE, proxy_type='Transfer', delay_blocks=0,
                secret_type='bittensor-wallet', signer_ref='wallet:my-delegate',
                signer_socket='/tmp/noah-signer.sock')


def test_delegate_wallet_address_checked_before_unlock():
    opened=[]
    class Wallet:
        def __init__(self, **kwargs):
            opened.append(kwargs)
        coldkeypub=SimpleNamespace(ss58_address=DELEGATE)
        @property
        def coldkey(self):
            opened.append('unlock delegate')
            return self.coldkeypub
    cfg=replace(config(),signing=settings())
    with patch.dict('sys.modules',{'bittensor':SimpleNamespace(Wallet=Wallet)}):
        assert load_delegate(cfg).ss58_address==DELEGATE
        assert opened[0]['name']=='my-delegate'
        assert opened[-1]=='unlock delegate'
        Wallet.coldkeypub=SimpleNamespace(ss58_address=cfg.coldkey)
        opened.clear()
        with pytest.raises(DepositRefused):load_delegate(cfg)
        assert len(opened)==1  # Public mismatch refused before unlocking anything.


@pytest.mark.parametrize('ref',['wallet:../owner','wallet:','wallet:/tmp/key','local-test:test'])
def test_wallet_reference_cannot_name_a_path_or_other_backend(ref):
    with pytest.raises(DepositRefused):validate_signing(dict(settings(),signer_ref=ref))


def test_simple_ini_policy_keeps_conversion_disabled(tmp_path):
    path=tmp_path/'proxy.ini'
    path.write_text('[signer]\npolicy_id = personal\nruntime_code_hash = 0x'+'a'*64+
                    '\nspec_version = 478\nmax_amount_rao = 10000000\n'
                    'max_total_rao = 20000000\nmax_fee_rao = 1000000\n')
    policy=ini_policy(path)
    assert policy['max_total_rao']==20000000
    assert policy['conversion']['enabled'] is False
    path.write_text(path.read_text()+'unexpected = any-call\n')
    with pytest.raises(DepositRefused):ini_policy(path)


@pytest.mark.parametrize('chain_name', ['MAINNET', 'TESTNET'])
def test_local_test_delegate_refuses_public_genesis_before_key_read(monkeypatch, tmp_path, chain_name):
    from types import SimpleNamespace
    from noah.core import chain
    from noah.deposits.assistant import signer
    from noah.chain.sources import Refusal

    monkeypatch.setattr(signer, 'signing', lambda _: {'signer_ref': 'local-test:fixture'})
    monkeypatch.setattr(signer, 'secret_backend', lambda _: 'local-test')
    cfg = SimpleNamespace(network='private-test', genesis_hash=getattr(chain, chain_name))
    with pytest.raises(Refusal, match='explicit private-test configuration'):
        signer.load_delegate(cfg, local_key_dir=tmp_path / 'absent')


@pytest.mark.parametrize('backend', ['remote-vault', 'managed-store', '', None])
def test_public_signer_rejects_unsupported_backends(backend):
    with pytest.raises(DepositRefused, match='unsupported secret_type'):
        validate_signing(dict(settings(), secret_type=backend))


def test_public_default_is_local_wallet_and_rejects_bare_references():
    from noah.deposits.assistant.proxy_signing import secret_backend
    values = settings()
    values.pop('secret_type')
    assert secret_backend(replace(config(), signing=values)) == 'bittensor-wallet'
    with pytest.raises(DepositRefused):
        validate_signing(dict(values, signer_ref='remote-reference'))
    with pytest.raises(DepositRefused):
        validate_signing(dict(values, secret_region='example'))
