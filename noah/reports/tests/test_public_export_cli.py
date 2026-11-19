"""Offline command boundary and recovery on synthetic adjudication results.

The default adjudicator import is substituted at the authenticated result
boundary. Real raw-package reconstruction is separately qualified by the
adjudicator's integration tests; these do not assert Mainnet activation.
"""
import argparse
from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import patch

from noah.reports import public_export as export
from noah.reports import public_export_cli as command
from noah.reports.tests import test_public_export as fixtures


class ExportCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublicExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        case = self.fixture
        self.request = case.root / 'request.json'
        self.request.write_text(json.dumps(case.request, indent=2) + '\n')
        self.receipt = case.root / 'private' / 'export.json'
        self.args = argparse.Namespace(request=self.request, package=case.package,
            manifest_sha256=case.manifest_sha, round_sha256=case.round_sha,
            activation_receipt=case.receipt_path, activation_receipt_sha256=case.receipt_sha,
            published_at=case.published_at, supersedes=None, out=case.output, receipt=self.receipt)
        module = types.ModuleType('noah.reports.adjudication')
        module.verify_adjudication = case.verify
        self.patch = patch.dict(sys.modules, {'noah.reports.adjudication': module})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.clock = patch.object(export.time, 'time', return_value=case.now_ms / 1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def run_export(self):
        return command.execute(self.args)

    def test_required_flags_and_explicit_timestamp(self):
        parser = command.add_arguments(argparse.ArgumentParser())
        required = ('request', 'package', 'manifest_sha256', 'round_sha256',
                    'activation_receipt', 'activation_receipt_sha256', 'published_at', 'out', 'receipt')
        argv = [item for key in required for item in ('--' + key.replace('_', '-'), str(getattr(self.args, key)))]
        parsed = parser.parse_args(argv)
        self.assertEqual(vars(parsed), vars(self.args))
        for position in range(0, len(argv), 2):
            with self.subTest(missing=argv[position]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    parser.parse_args(argv[:position] + argv[position + 2:])
                self.assertEqual(stopped.exception.code, 2)
        self.args.published_at = None
        with self.assertRaises(export.PublicExportError):
            self.run_export()
        self.assertFalse(self.receipt.exists())

    def test_receipt_binds_exact_inputs_source_and_actual_public_bytes(self):
        with patch.object(export, 'export_public_round', wraps=export.export_public_round) as guarded:
            result = self.run_export()
        self.assertNotIn('verifier', guarded.call_args.kwargs)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(result['status'], 'PREPARED')
        self.assertFalse(result['publication_performed'])
        self.assertTrue(result['private_receipt_created'])
        self.assertTrue(result['public_file_created'])
        receipt = json.loads(self.receipt.read_bytes())
        self.assertEqual(receipt['state'], 'VERIFIED_EXPORT_INTENT')
        self.assertEqual(receipt['request_file_sha256'], hashlib.sha256(self.request.read_bytes()).hexdigest())
        self.assertEqual(receipt['request_sha256'], export._digest(self.fixture.request))
        self.assertNotEqual(receipt['request_file_sha256'], receipt['request_sha256'])
        self.assertEqual(receipt['policy_sha256'], self.fixture.request['policy_sha256'])
        self.assertEqual(receipt['round_sha256'], self.fixture.round_sha)
        self.assertEqual(receipt['manifest_sha256'], self.fixture.manifest_sha)
        self.assertEqual(receipt['activation_receipt_sha256'], self.fixture.receipt_sha)
        self.assertEqual(receipt['exporter_source_sha256'], export._digest(receipt['exporter_source_files']))
        for name, digest in receipt['exporter_source_files'].items():
            self.assertEqual(digest, hashlib.sha256((Path(command.__file__).parent / name).read_bytes()).hexdigest())
        actual = Path(result['public_file']).read_bytes()
        self.assertEqual(receipt['public_file_sha256'], hashlib.sha256(actual).hexdigest())
        self.assertEqual(receipt['public_file_bytes'], len(actual))
        self.assertEqual(result['private_receipt_sha256'], hashlib.sha256(self.receipt.read_bytes()).hexdigest())
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.receipt.parent.stat().st_mode & 0o777, 0o700)
        for secret in (b'exporter_source', b'activation_receipt', b'request_file', b'SECRET_EXPORT_CANARY'):
            self.assertNotIn(secret, actual)

    def test_retry_reverifies_and_preserves_both_immutable_files(self):
        first = self.run_export()
        before = self.receipt.read_bytes(), Path(first['public_file']).read_bytes()
        second = self.run_export()
        self.assertFalse(second['private_receipt_created'])
        self.assertFalse(second['public_file_created'])
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertEqual(before, (self.receipt.read_bytes(), Path(first['public_file']).read_bytes()))

    def test_crash_after_intent_can_resume_without_false_completion(self):
        with patch.object(export, '_write_export', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.run_export()
        self.assertEqual(json.loads(self.receipt.read_bytes())['state'], 'VERIFIED_EXPORT_INTENT')
        self.assertFalse(self.fixture.output.exists())
        result = self.run_export()
        self.assertFalse(result['private_receipt_created'])
        self.assertTrue(result['public_file_created'])

    def test_private_receipt_failure_creates_no_public_artifact(self):
        with patch.object(command, '_retain_receipt', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.run_export()
        self.assertFalse(self.fixture.output.exists())

    def test_changed_request_bytes_or_timestamp_cannot_reuse_receipt(self):
        first = self.run_export()
        original = self.request.read_bytes()
        self.request.write_bytes(original + b'\n')
        with self.assertRaisesRegex(export.PublicExportError, 'existing private export receipt differs'):
            self.run_export()
        self.request.write_bytes(original)
        self.args.published_at = '2026-11-15T02:00:00Z'
        self.args.supersedes = first['report_id']
        with self.assertRaisesRegex(export.PublicExportError, 'existing private export receipt differs'):
            self.run_export()
        self.assertEqual(len(list(self.fixture.output.glob('*.json'))), 1)
        self.args.receipt = self.receipt.parent / 'correction.json'
        self.assertTrue(self.run_export()['public_file_created'])
        self.assertEqual(len(list(self.fixture.output.glob('*.json'))), 2)

    def test_replay_is_network_blocked_and_original_socket_methods_restored(self):
        old_connect = socket.socket.connect
        base = self.fixture.verify
        def verifier(*args, **kwargs):
            with socket.socket() as sock:
                with self.assertRaisesRegex(export.PublicExportError, 'offline network'):
                    sock.connect(('127.0.0.1', 1))
                with self.assertRaises(export.PublicExportError):
                    sock.connect_ex(('127.0.0.1', 1))
            with self.assertRaises(export.PublicExportError):
                socket.getaddrinfo('synthetic.invalid', 443)
            return base(*args, **kwargs)
        sys.modules['noah.reports.adjudication'].verify_adjudication = verifier
        self.run_export()
        self.assertIs(socket.socket.connect, old_connect)

    def test_failed_replay_leaves_no_intent_or_public_output_and_restores_network(self):
        original = socket.socket.connect
        def refused(*_args, **_kwargs):
            raise ValueError('synthetic bad proof')
        sys.modules['noah.reports.adjudication'].verify_adjudication = refused
        with self.assertRaises(ValueError):
            self.run_export()
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.fixture.output.exists())
        self.assertIs(socket.socket.connect, original)

    def test_source_change_during_replay_prevents_receipt_and_export(self):
        with patch.object(command, '_sources', side_effect=[{'source': 'a' * 64}, {'source': 'b' * 64}]):
            with self.assertRaisesRegex(export.PublicExportError, 'source files changed'):
                self.run_export()
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.fixture.output.exists())

    def test_private_paths_cannot_overlap_public_output_or_pinned_package(self):
        cases = [('receipt', self.fixture.output / 'receipt.json'),
                 ('receipt', self.fixture.package / 'receipt.json'),
                 ('receipt', self.request), ('out', self.fixture.root),
                 ('out', self.fixture.package / 'public'),
                 ('out', self.receipt.parent)]
        for field, value in cases:
            original = getattr(self.args, field)
            setattr(self.args, field, value)
            with self.subTest(field=field, path=value), self.assertRaises(export.PublicExportError):
                self.run_export()
            setattr(self.args, field, original)
        self.assertEqual(self.fixture.calls, [])

    def test_request_duplicate_keys_symlink_and_size_refused_before_replay(self):
        original = self.request.read_bytes()
        for raw in (b'{"schema":"first","schema":"second"}', b' ' * (command.MAX_REQUEST_BYTES + 1)):
            self.request.write_bytes(raw)
            with self.assertRaises(export.PublicExportError):
                self.run_export()
        other = self.fixture.root / 'other.json'
        other.write_bytes(original)
        self.request.unlink()
        self.request.symlink_to(other)
        with self.assertRaises(export.PublicExportError):
            self.run_export()
        self.assertEqual(self.fixture.calls, [])

    def test_tampered_symlink_or_nonprivate_existing_receipt_refused(self):
        self.run_export()
        raw = self.receipt.read_bytes()
        self.receipt.write_bytes(raw + b'\n')
        with self.assertRaises(export.PublicExportError):
            self.run_export()
        self.receipt.write_bytes(raw)
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(export.PublicExportError, 'permissions'):
            self.run_export()
        self.receipt.chmod(0o600)
        other = self.receipt.parent / 'retained.json'
        self.receipt.rename(other)
        self.receipt.symlink_to(other)
        with self.assertRaises(export.PublicExportError):
            self.run_export()


if __name__ == '__main__':
    unittest.main()
