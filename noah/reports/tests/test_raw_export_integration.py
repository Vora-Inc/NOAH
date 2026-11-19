"""Synthetic raw SCALE → actual provider replay → default public exporter.

Only the JSON-RPC transport is replaced. The actual SDK, computation, roster,
finality proof, selection, adjudicator, safe package snapshot and exporter run.
Finney/SN82 are fixture domain values, NOT observed Mainnet facts. No live chain,
real signing, activation, publishing or production report index is involved.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.reports.adjudication import adjudicate, verify_adjudication, _inventory
from noah.reports.artifacts import canonical_bytes
from noah.reports.budget import ProviderBudget
from noah.reports.common import digest, ReportError
from noah.reports.public_export import PublicExportError, GENESIS, POLICY
from noah.reports.public_export_cli import execute
from noah.reports.backing.rpc import result_sha256
from noah.reports.backing.keys import blake2_128_concat, hex_encode
from noah.reports.roster import _prefix
from noah.reports.tests.raw_export_fixture import RawChain, RawSocket, block_hash, HOT, COLD, ARK, T0


def build(root, *, disagree=False, context=False, assignments=False, assigned_missing=False):
    root = root.resolve()
    root.mkdir(parents=True)
    readers = {name: RawChain(context=context, unassigned=assignments, assigned_missing=assigned_missing)
               for name in ('reader_a', 'reader_b')}
    if disagree:
        readers['reader_b'].blocks[block_hash(3)]['block']['extrinsics'].pop()
    request = readers['reader_a'].request()
    if assignments:
        request['assignments'] = [dict(uid=0, hotkey=HOT.hex(), coldkey=COLD.hex(),
                                       registration_block=1, targets=[82])]
    endpoints = {f'ws://{name}.invalid': value for name, value in readers.items()}
    invocation = {'schema': 'noah.report-adjudication-invocation/1', 'request': request,
        'sources': [{'id': name, 'endpoint': f'ws://{name}.invalid', 'history': None} for name in readers]}
    def raw_socket(budget, endpoint):
        value = RawSocket(endpoints[endpoint], budget)
        budget.connections.append(value)
        return value
    with patch.object(ProviderBudget, 'socket', raw_socket), \
            patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden in synthetic fixture')):
        result = adjudicate(invocation, root / 'package')
    # Only source JSON-RPC replies were substituted. No decoder or semantic
    # result object enters the default verifier below via a test callback.
    (root / 'request.json').write_bytes(canonical_bytes(request))
    approval = {'schema': 'noah.report-publication-activation/1', 'genesis_hash': GENESIS,
        'mining_netuid': 82, 'policy_sha256': request['policy_sha256'],
        'eligibility_policy_version': POLICY, 'effective_day': T0 // 86400000,
        'approved_at': '2026-11-14T23:00:00Z', 'report_enforcement': True,
        'publication_approved': True,
        'approval_reference': 'SYNTHETIC RAW-SCALE INTEGRATION ONLY; NOT MAINNET OR ACTIVATION PROOF'}
    (root / 'activation.json').write_bytes(canonical_bytes(approval))
    args = SimpleNamespace(request=root / 'request.json', package=root / 'package',
        manifest_sha256=result['manifest_sha256'], round_sha256=digest(readers['reader_a'].contract),
        activation_receipt=root / 'activation.json',
        activation_receipt_sha256=hashlib.sha256((root / 'activation.json').read_bytes()).hexdigest(),
        published_at='2026-11-15T01:00:00Z', supersedes=None, out=root / 'public', receipt=root / 'private/export.json')
    return SimpleNamespace(root=root, args=args, request=request, result=result, readers=readers)


@pytest.fixture(scope='module')
def complete(tmp_path_factory):
    return build(tmp_path_factory.mktemp('raw-export-integration').resolve() / 'complete')


def clone(complete, tmp_path):
    root = tmp_path.resolve() / 'clone'
    root.mkdir()
    shutil.copytree(complete.args.package, root / 'package')
    return root / 'package'


def repin(root):
    # An adversary can recompute checksums; that must not make a changed saved
    # verdict or raw proof authoritative. Preserve semantic claims deliberately.
    for path in sorted(root.glob('rounds/*/reader_*/manifest.json')):
        document = json.loads(path.read_bytes())
        document['files'] = _inventory(path.parent)
        path.write_bytes(canonical_bytes(document))
    path = root / 'manifest.json'
    document = json.loads(path.read_bytes())
    document['files'] = _inventory(root)
    path.write_bytes(canonical_bytes(document))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_raw_provider_and_default_exporter_reconstruct_without_core_mocks(complete):
    assert complete.result['status'] == 'COMPLETE'
    assert complete.result['scored_decisions'][0]['outcome'] == 'verified'
    for reader in complete.readers.values():
        assert any(method == 'state_call' for method, _ in reader.calls)
        assert any(method == 'state_getKeysPaged' for method, _ in reader.calls)
        assert any(method == 'chain_getBlock' for method, _ in reader.calls)
        # The real installed SDK selects its advertised alias. The direct
        # roster/engine reader still uses state_getStorage, so replay must
        # reconcile both spellings without weakening pinned-read checks.
        assert any(method == 'state_getStorageAt' for method, _ in reader.calls)
        assert any(method == 'state_getStorage' for method, _ in reader.calls)
    for path in complete.args.package.glob('rounds/*/reader_*/collection/responses.jsonl.gz'):
        rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        assert any(row['method'] == 'state_getStorageAt' for row in rows)
    # No callback argument, monkeypatch or network transport is active here.
    output = execute(complete.args)
    assert output['status'] == 'PREPARED' and output['publication_performed'] is False
    report = json.loads(Path(output['public_file']).read_bytes())
    assert report['report_id'] == 'ee0d98f0f30521f24e9456d0a58632a05c811bb1baac0d035d6f2bc6776a285e'
    assert report['verification']['status'] == 'verified'
    assert report['verification']['required_submissions'] == report['verification']['counts']['VERIFIED'] == 1
    expected = complete.readers['reader_a'].expected_engine
    assert report['calculation']['output1']['drop_ppm'] == str(expected['output1']['drop_ppm'])
    assert report['calculation']['output2']['alpha_required_rao'] == str(expected['output2']['alpha_required_rao'])
    assert report['calculation']['output3']['alpha_required_rao'] == str(expected['output3']['alpha_required_rao'])
    assert (report['calculation']['output1']['drop_ppm'],
            report['calculation']['output2']['alpha_required_rao'],
            report['calculation']['output3']['alpha_required_rao']) == ('42307', '5000000000000', '11111112000000')
    public = Path(output['public_file']).read_text()
    assert all(secret not in public for secret in (HOT.hex(), COLD.hex(), ARK.hex(), 'reader_a', 'approval_reference', 'SYNTHETIC'))
    receipt = json.loads(complete.args.receipt.read_bytes())
    assert receipt['state'] == 'VERIFIED_EXPORT_INTENT'
    assert receipt['manifest_sha256'] == complete.result['manifest_sha256']
    assert receipt['public_file_sha256'] == hashlib.sha256(public.encode()).hexdigest()


def test_default_exporter_bytes_pass_separate_website_consumer(complete):
    repo = Path(os.environ.get('NOAH_REPORT_WEBSITE_ROOT', Path(__file__).resolve().parents[5]))
    tool = repo / 'apps/website/tools/validate-backing-export.mjs'
    if not tool.is_file() or shutil.which('node') is None:
        pytest.skip('separately reviewed website checkout unavailable; set NOAH_REPORT_WEBSITE_ROOT')
    output = execute(complete.args)
    result = subprocess.run(['node', str(tool), output['public_file']], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['selected'] == [{'netuid': 82, 'status': 'verified'}]
    assert response['reports'] == [output['report_id']]


def test_rehashed_saved_final_verdict_cannot_override_raw_sdk_replay(complete, tmp_path):
    root = clone(complete, tmp_path)
    path = next(root.glob('rounds/*/final-verdicts.json'))
    value = json.loads(path.read_bytes())
    value['rows'][0].update(status='MISSING', reason='reveal', outcome='confirmed_failure', effect='zero')
    path.write_bytes(canonical_bytes(value))
    with pytest.raises(ReportError, match='reconstructed artifact differs'):
        verify_adjudication(root, complete.request, repin(root))


def test_rehashed_raw_block_change_cannot_reuse_previous_verdict(complete, tmp_path):
    root = clone(complete, tmp_path)
    path = next(root.glob('rounds/*/reader_a/collection/responses.jsonl.gz'))
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    changed = 0
    for row in rows:
        if row['method'] == 'chain_getBlock' and row['params'] == [block_hash(3)]:
            row['result']['block']['extrinsics'] = row['result']['block']['extrinsics'][:1]
            row['sha256'], row['byte_length'] = result_sha256(row['result'])
            changed += 1
    assert changed
    path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows), mtime=0))
    with pytest.raises(ReportError, match='reconstructed artifact differs'):
        verify_adjudication(root, complete.request, repin(root))


def test_rehashed_cutoff_claim_is_checked_against_raw_timestamp_storage(complete, tmp_path):
    root = clone(complete, tmp_path)
    path = next(root.glob('rounds/*/reader_a/collection/evaluation.json'))
    value = json.loads(path.read_bytes())
    value['evaluation']['timestamp_ms'] += 1
    path.write_bytes(canonical_bytes(value))
    with pytest.raises(ReportError, match='does not reconstruct'):
        verify_adjudication(root, complete.request, repin(root))


def test_sdk_storage_alias_cannot_contradict_same_reader_roster_storage(complete, tmp_path):
    root = clone(complete, tmp_path)
    path = next(root.glob('rounds/*/reader_a/collection/responses.jsonl.gz'))
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    uid_key = hex_encode(_prefix('Uids', 82) + blake2_128_concat(HOT))
    changed = 0
    for row in rows:
        if row['method'] == 'state_getStorageAt' and row['params'] == [uid_key, block_hash(2)]:
            # Both UID values decode as registered. Only cross-stream alias
            # consistency detects that this reader has contradicted its roster.
            assert row['result'] == '0x0000'
            row['result'] = '0x0100'
            row['sha256'], row['byte_length'] = result_sha256(row['result'])
            changed += 1
    assert changed
    path.write_bytes(gzip.compress(b''.join(canonical_bytes(row) for row in rows), mtime=0))
    with pytest.raises(ReportError, match='contradicts immutable cross-session response'):
        verify_adjudication(root, complete.request, repin(root))


def test_independent_raw_reader_conflict_stays_unresolved_and_cannot_export(tmp_path):
    case = build(tmp_path.resolve() / 'disagreement', disagree=True)
    assert case.result['status'] == 'UNRESOLVED'
    assert case.result['scored_decisions'][0]['outcome'] == 'unresolved'
    assert case.result['scored_decisions'][0]['effect'] == 'preserve'
    with pytest.raises(PublicExportError, match='round must be resolved'):
        execute(case.args)
    assert not case.args.receipt.exists() and not case.args.out.exists()


@pytest.mark.parametrize('assigned_missing', [False, True])
def test_context_assignment_export_counts_only_assigned_incarnation(tmp_path, assigned_missing):
    case = build(tmp_path / 'context-assignment', context=True, assignments=True,
                 assigned_missing=assigned_missing)
    output = execute(case.args)  # Default verifier replays real raw-SCALE evidence.
    report = json.loads(Path(output['public_file']).read_bytes())
    replay = verify_adjudication(case.args.package, case.request,
                                case.result['manifest_sha256'], include_round_artifacts=True)
    artifact = replay['round_artifacts'][0]
    assert len(artifact['required_roster']['snapshot']['members']) == 2
    assert len(artifact['verdicts']['rows']) == 2  # Never discard unassigned proof.
    assert report['round']['round_sha256'] == digest(artifact['contract'])
    assert report['calculation']['engine_sha256'] == hashlib.sha256(canonical_bytes(artifact['engine'])).hexdigest()
    assert report['verification']['verdicts_sha256'] == hashlib.sha256(canonical_bytes(artifact['verdicts'])).hexdigest()
    assert report['report_id'] == digest({k: v for k, v in report.items() if k != 'report_id'})
    verification = report['verification']
    assert verification['required_submissions'] == 1
    assert verification['counts']['VERIFIED'] == int(not assigned_missing)
    assert verification['counts']['MISSING'] == int(assigned_missing)
    status = 'not_verified' if assigned_missing else 'verified'
    assert verification['status'] == status
    public = Path(output['public_file']).read_text()
    assert all(private not in public for private in
               ('assignments', 'assignment_signer_public_key', 'signature', 'approval_reference',
                'reader_a', HOT.hex(), COLD.hex(), ARK.hex()))
    repo = Path(os.environ.get('NOAH_REPORT_WEBSITE_ROOT', Path(__file__).resolve().parents[5]))
    tool = repo / 'apps/website/tools/validate-backing-export.mjs'
    if not tool.is_file() or shutil.which('node') is None:
        pytest.skip('separately reviewed website checkout unavailable; set NOAH_REPORT_WEBSITE_ROOT')
    consumed = subprocess.run(['node', str(tool), output['public_file']], capture_output=True, text=True)
    assert consumed.returncode == 0, consumed.stderr
    assert json.loads(consumed.stdout)['selected'] == [{'netuid': 82, 'status': status}]
