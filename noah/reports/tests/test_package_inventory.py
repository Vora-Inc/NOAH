"""Structural package validation only: no chain/verdict or live backup claims."""
from dataclasses import replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from noah.reports import package_inventory as rp


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ReportPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'package'
        self.root.mkdir()
        self.entries = []
        self.limits = rp.ReportPackageLimits(
            manifest_bytes=64*1024, member_bytes=1024*1024,
            expanded_member_bytes=1024*1024, total_bytes=4*1024*1024,
            total_expanded_bytes=4*1024*1024, members=100, path_depth=5,
            json_depth=20, line_bytes=64*1024)

    def add(self, path, data=b'{"ok":true}\n', kind='json'):
        encoding = rp.KIND_ENCODINGS[kind]
        stored = gzip.compress(data, mtime=0) if encoding == 'gzip' else data
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stored)
        entry = dict(path=path, kind=kind, encoding=encoding,
                     sha256=digest(stored), bytes=len(stored),
                     expanded_sha256=digest(data), expanded_bytes=len(data))
        self.entries.append(entry)
        return entry

    def manifest(self):
        # Deliberately not an adjudication schema; only the byte primitive is
        # exercised. A saved COMPLETE/verdict field is never treated as proof.
        raw = json.dumps({'files': self.entries, 'status': 'UNTRUSTED'}, sort_keys=True).encode()
        (self.root / 'manifest.json').write_bytes(raw)
        return digest(raw)

    def check(self, **kwargs):
        return rp.validate_package_inventory(self.root, manifest_sha256=self.manifest(),
                                             limits=kwargs.pop('limits', self.limits), **kwargs)

    def test_exact_complete_package_and_nested_manifest(self):
        self.add('reader-a/manifest.json', b'{"files":[]}\n', 'manifest')
        self.add('reader-a/responses.jsonl.gz', b'{"raw":"x"}\n', 'rpc_responses')
        self.add('segments/000000000000-000000000001.jsonl.gz', b'{"block":0}\n', 'history_segment')
        self.add('receipts.jsonl.gz', b'', 'receipts')
        self.add('metadata.hex', b'0x00aF', 'metadata')
        result = self.check()
        self.assertTrue(result['structural_inventory_verified'])
        self.assertEqual(result['members'], 5)
        self.assertNotIn('status', result)

    def test_manifest_pin_and_duplicate_json_keys_refuse(self):
        self.add('a.json')
        pin = self.manifest()
        (self.root/'manifest.json').write_bytes(b'{"files":[],"files":[]}')
        for sha in (pin, digest((self.root/'manifest.json').read_bytes())):
            with self.subTest(sha=sha), self.assertRaises(rp.ReportPackageRefused):
                rp.validate_package_inventory(self.root, manifest_sha256=sha, limits=self.limits)

    def test_entries_are_exact_and_typed(self):
        entry = self.add('a.json')
        original = entry.copy()
        changes = [('bytes', True), ('expanded_bytes', -1), ('sha256', 'A'*64),
                   ('expanded_sha256', '0'*64), ('encoding', 'gzip'),
                   ('kind', 'unknown'), ('kind', []), ('extra', 'unsupported')]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                entry.clear(); entry.update(original); entry[key] = value
                with self.assertRaises(rp.ReportPackageRefused): self.check()
        entry.clear(); entry.update(original); entry.pop('expanded_sha256')
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_missing_unlisted_duplicate_and_self_members_refuse(self):
        entry = self.add('a.json')
        (self.root/'unlisted.json').write_text('{}')
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        (self.root/'unlisted.json').unlink()
        self.entries.append(dict(entry))
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        self.entries.pop()
        (self.root/'a.json').unlink()
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        entry['path'] = 'manifest.json'
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_unsafe_paths_and_empty_extra_directory_refuse(self):
        entry = self.add('a.json')
        for value in ('../outside', '/absolute', 'a//b', './a.json', 'a/../b',
                      'a\\b', 'line\nname', '', 'a/b/c/d/e/f.json'):
            with self.subTest(value=value):
                entry['path'] = value
                with self.assertRaises(rp.ReportPackageRefused): self.check()
        entry['path'] = 'a.json'
        (self.root/'extra').mkdir()
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_symlinks_hardlinks_and_fifo_refuse(self):
        self.add('a.json')
        file = self.root/'a.json'; outside = self.root.parent/'original'
        file.rename(outside)
        file.symlink_to(outside)
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        file.unlink(); os.link(outside, file)
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        file.unlink(); os.mkfifo(file)
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_parent_directory_symlink_refuses(self):
        self.add('nested/a.json')
        moved = self.root.parent/'moved'
        (self.root/'nested').rename(moved)
        (self.root/'nested').symlink_to(moved, target_is_directory=True)
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        pin = self.manifest()
        alias = self.root.parent/'alias'; alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(rp.ReportPackageRefused):
            rp.validate_package_inventory(alias, manifest_sha256=pin, limits=self.limits)

    def test_member_count_stored_hash_and_replaced_root_refuse(self):
        self.add('a.json'); self.add('b.json')
        with self.assertRaises(rp.ReportPackageRefused): self.check(limits=replace(self.limits, members=1))
        (self.root/'a.json').write_bytes(b'{"ok":null}\n')
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        (self.root/'a.json').write_bytes(b'{"ok":true}\n')
        original = rp._check_member
        moved = self.root.parent/'renamed'
        def moved_root(*args):
            original(*args)
            if self.root.exists():
                self.root.rename(moved)
                self.root.mkdir()
        with patch.object(rp, '_check_member', side_effect=moved_root), self.assertRaises(rp.ReportPackageRefused):
            self.check()

    def test_raw_and_expanded_digests_are_separate(self):
        entry = self.add('responses.gz', b'{"value":1}\n', 'rpc_responses')
        self.assertNotEqual(entry['sha256'], entry['expanded_sha256'])
        self.check()
        entry['expanded_sha256'] = digest(b'{"value": 1}\n')
        with self.assertRaises(rp.ReportPackageRefused): self.check()
        entry['expanded_sha256'] = digest(b'{"value":1}\n')
        path = self.root/'responses.gz'; path.write_bytes(path.read_bytes()[:-2])
        entry.update(sha256=digest(path.read_bytes()), bytes=path.stat().st_size)
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_expansion_line_depth_and_aggregate_limits(self):
        entry = self.add('responses.gz', b'{"padding":"'+b'x'*200+b'"}\n', 'rpc_responses')
        self.check()
        for limits in (replace(self.limits, line_bytes=100), replace(self.limits, expanded_member_bytes=100),
                       replace(self.limits, total_bytes=10), replace(self.limits, total_expanded_bytes=10),
                       replace(self.limits, manifest_bytes=10), replace(self.limits, member_bytes=10)):
            with self.subTest(limits=limits), self.assertRaises(rp.ReportPackageRefused): self.check(limits=limits)
        entry['expanded_bytes'] = 100
        with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_json_syntax_depth_duplicate_keys_and_metadata_format(self):
        self.add('a.json')
        for data in (b'{"a":1,"a":2}', b'NaN', b'[[[[]]]]', b'\xff', b'{'):
            with self.subTest(data=data):
                self.entries.clear(); self.add('a.json', data)
                with self.assertRaises(rp.ReportPackageRefused): self.check(limits=replace(self.limits, json_depth=3))
        self.entries.clear(); (self.root/'a.json').unlink()
        for data in (b'0x0', b'0x', b'0xgg', b'00ab', b'0x00\n'):
            with self.subTest(data=data):
                self.entries.clear(); self.add('metadata.hex', data, 'metadata')
                with self.assertRaises(rp.ReportPackageRefused): self.check()

    def test_fresh_membership_recheck_refuses_mutation(self):
        self.add('a.json'); original = rp._check_member
        def changed(*args):
            original(*args)
            (self.root/'a.json').write_bytes(b'{"changed":true}')
        with patch.object(rp, '_check_member', side_effect=changed), self.assertRaises(rp.ReportPackageRefused):
            self.check()

    def test_limits_required_and_bad_limits_refused(self):
        self.add('a.json')
        with self.assertRaises(TypeError):
            rp.validate_package_inventory(self.root, manifest_sha256=self.manifest())
        for changes in ({'members':0}, {'line_bytes':True}, {'json_depth':129}, {'path_depth':33}):
            with self.assertRaises(rp.ReportPackageRefused): replace(self.limits, **changes)

    def test_jsonl_memory_bounded_independent_of_expanded_total(self):
        row = b'{"data":"' + b'x'*2048 + b'"}\n'
        self.add('responses.gz', row*4096, 'rpc_responses')
        limits = replace(self.limits, expanded_member_bytes=16*1024*1024, total_expanded_bytes=20*1024*1024)
        tracemalloc.start()
        try:
            self.check(limits=limits)
            _, peak = tracemalloc.get_traced_memory()
            self.assertLess(peak, 2*1024*1024)
        finally:
            tracemalloc.stop()


if __name__ == '__main__':
    unittest.main()
