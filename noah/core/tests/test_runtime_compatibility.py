"""Replay the exact 494 to 495 identity change and exercise signed admission."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from ...chain import runtime_upgrade, token_context
from ...chain.runtime_compatibility import Approval, RuntimeApprovalRequired, lookup, verify
from ...chain.runtime_upgrade import install, verify_wasm

GENESIS = '0x' + '01' * 32
METADATA = b'meta\x0f'
SOURCE = 'e536e6bf4bfa58f1ec4506dff0147e1f9b1c1900'
CODE = token_context.CODES[495]
APPROVAL = Approval(GENESIS, 495, 2, CODE, hashlib.sha256(METADATA).hexdigest(), SOURCE,
                    frozenset(('token', 'ema', 'weights')))


@pytest.fixture
def signed():
    ECC = pytest.importorskip('Crypto.PublicKey.ECC')
    from Crypto.Signature import eddsa

    key = ECC.generate(curve='Ed25519')

    def sign(*entries):
        lines = ['noah-runtime-approvals/1']
        for row in entries:
            lines.append('\t'.join((row.genesis, str(row.spec), str(row.transaction), row.code,
                                    row.metadata, row.source, ','.join(sorted(row.capabilities)))))
        payload = ('\n'.join(lines) + '\n').encode()
        return eddsa.new(key, 'rfc8032').sign(payload).hex().encode() + b'\n' + payload

    return key.public_key().export_key(format='raw').hex(), sign


def test_exact_capability_and_atomic_append(tmp_path, monkeypatch, signed):
    key, sign = signed
    path = tmp_path/'approvals'
    monkeypatch.setenv('NOAH_RUNTIME_APPROVALS', str(path))
    monkeypatch.setenv('NOAH_RUNTIME_APPROVAL_KEY', key)
    assert lookup(GENESIS, 495, 2, CODE, 'token') is None
    assert install(path, sign(APPROVAL), key) == 1
    assert lookup(GENESIS, 495, 2, CODE, 'token') == APPROVAL
    assert lookup(GENESIS, 495, 2, CODE, 'reports') is None
    assert lookup('0x' + '02' * 32, 495, 2, CODE, 'ema') is None
    assert lookup(GENESIS, 495, 1, CODE, 'ema') is None
    assert lookup(GENESIS, 495, 2, '0x' + '02' * 32, 'ema') is None
    extended = replace(APPROVAL, capabilities=APPROVAL.capabilities | {'reports'})
    assert install(path, sign(extended), key) == 1
    sealed = path.read_bytes()
    for invalid in (sign(APPROVAL), sign(extended) + b'x',
                    sign(replace(extended, code='0x' + '03' * 32)), sign(extended, extended)):
        with pytest.raises(ValueError):
            install(path, invalid, key)
        assert path.read_bytes() == sealed
    with pytest.raises(ValueError):
        verify(sealed, '00' * 32)


def test_replay_upgrade_boundary_without_built_in_495(tmp_path, monkeypatch, signed):
    key, sign = signed
    path = tmp_path/'approvals'
    monkeypatch.setenv('NOAH_RUNTIME_APPROVALS', str(path))
    monkeypatch.setenv('NOAH_RUNTIME_APPROVAL_KEY', key)
    monkeypatch.delitem(token_context.CODES, 495)
    hashes = {'before': token_context.CODES[494], 'after': CODE}
    versions = {'before': 494, 'after': 495}

    def rpc(_, method, parameters):
        at = parameters[-1]
        if method == 'chain_getBlockHash':
            return GENESIS
        if method == 'state_getRuntimeVersion':
            return {'specVersion': versions[at], 'transactionVersion': 2}
        if method == 'state_getStorageHash':
            return hashes[at]
        raise AssertionError(method)

    monkeypatch.setattr(token_context, 'rpc', rpc)
    monkeypatch.setattr(token_context, 'explicit_metadata', lambda *_: METADATA)
    assert token_context.runtime_identity(None, 'before')['spec_version'] == 494
    with pytest.raises(RuntimeApprovalRequired):
        token_context.runtime_identity(None, 'after')
    install(path, sign(APPROVAL), key)
    assert token_context.runtime_identity(None, 'before')['spec_version'] == 494
    assert token_context.runtime_identity(None, 'after')['spec_version'] == 495


def test_qualification_requires_exact_wasm(tmp_path):
    wasm = tmp_path/'runtime.wasm'
    wasm.write_bytes(b'candidate')
    identity = {'code': '0x' + hashlib.blake2b(b'candidate', digest_size=32).hexdigest()}
    verify_wasm(wasm, identity)
    wasm.write_bytes(b'other runtime')
    with pytest.raises(ValueError, match='exact on-chain code hash'):
        verify_wasm(wasm, identity)


def test_observer_emits_structured_refusal(tmp_path, capsys):
    assert runtime_upgrade.main(['observe', '--env', str(tmp_path/'absent')]) == 1
    stdout, stderr = capsys.readouterr()
    assert stdout == ''
    assert 'read_or_verification_failed' in stderr and 'FileNotFoundError' in stderr


@pytest.mark.parametrize('divergence', [False, True])
def test_unsigned_rehearsal_can_read_unapproved_runtime(tmp_path, monkeypatch, divergence):
    pytest.importorskip('Crypto.PublicKey.ECC')
    wasm = tmp_path/'runtime.wasm'
    wasm.write_bytes(b'candidate')
    code = '0x' + hashlib.blake2b(b'candidate', digest_size=32).hexdigest()
    prior = tmp_path/'trusted-registry'
    prior.write_bytes(b'unchanged')
    monkeypatch.setenv('NOAH_RUNTIME_APPROVALS', str(prior))
    monkeypatch.setenv('NOAH_RUNTIME_APPROVAL_KEY', '00' * 32)
    monkeypatch.setattr(runtime_upgrade, 'independent_connections', lambda *_: (0, 1))
    monkeypatch.setattr(runtime_upgrade, 'finalized', lambda _: 12)

    def identity(_, genesis, height):
        return dict(genesis=genesis, height=height, block=f'0x{height:064x}', transaction=2,
                    code=code if height > 9 else CODE, spec=496 if height > 9 else 495,
                    metadata=APPROVAL.metadata)

    temporary_paths = []

    def observe(reader, observed, *_):
        fields = (GENESIS, 496, 2, code)
        if lookup(*fields, 'token') is None:
            raise RuntimeApprovalRequired('token')
        temporary_paths.append(Path(os.environ['NOAH_RUNTIME_APPROVALS']))
        assert observed['height'] == 11
        assert all(lookup(*fields, capability).source == SOURCE
                   for capability in ('burn', 'ema', 'reports', 'token'))
        assert lookup(*fields, 'weights') is None
        return {'value': reader if divergence else 1}

    monkeypatch.setattr(runtime_upgrade, 'paired_identity', identity)
    monkeypatch.setattr(runtime_upgrade, 'rehearsal_observation', observe)
    settings = type('Settings', (), {'chain_endpoint': 'left', 'second_chain_endpoint': 'right'})()
    arguments = (settings, GENESIS, 10, wasm, 2, 3, 'unused')
    with pytest.raises(RuntimeApprovalRequired):
        runtime_upgrade.rehearse(*arguments)
    if divergence:
        with pytest.raises(ValueError, match='probes diverge'):
            runtime_upgrade.rehearse(*arguments, source=SOURCE)
    else:
        result = runtime_upgrade.rehearse(*arguments, source=SOURCE)
        assert result['rehearsal_source'] == SOURCE
        assert result['status'] == 'paired_unsigned_rehearsal'
        assert result['capabilities'] == []
    assert len(temporary_paths) == 2 and all(not path.exists() for path in temporary_paths)
    assert os.environ['NOAH_RUNTIME_APPROVALS'] == str(prior) and prior.read_bytes() == b'unchanged'
    assert os.environ['NOAH_RUNTIME_APPROVAL_KEY'] == '00' * 32


def test_typescript_independently_validates_signed_approval(tmp_path, signed):
    key, sign = signed
    path = tmp_path/'approval'
    path.write_bytes(sign(APPROVAL))
    typescript = Path(__file__).resolve().parents[3]/'verification/noah-ts/src/runtime_compatibility.ts'
    script = '''
        const { approvedMetadata } = await import(process.argv[1]);
        const [genesis, code, expected] = process.argv.slice(2);
        if (approvedMetadata(genesis, 495, 2, code, 'ema') !== expected
            || approvedMetadata(genesis, 495, 2, code, 'reports') !== undefined) {
            process.exit(2);
        }
    '''
    def check(expected):
        return subprocess.run(
            ['node', '--input-type=module', '-e', script, typescript.as_uri(),
             GENESIS, CODE, expected],
            env={'PATH': os.environ['PATH'], 'NOAH_RUNTIME_APPROVALS': str(path),
                 'NOAH_RUNTIME_APPROVAL_KEY': key},
            check=False,
        ).returncode
    assert check(APPROVAL.metadata) == 0
    path.write_bytes(path.read_bytes() + b'x')
    assert check(APPROVAL.metadata) == 2
