"""Caller-owned evidence may change; decoders only receive bounded private copies."""
import hashlib
import os

import pytest

from noah.reports.adjudication import LIMITS, _inventory, _write
from noah.reports.package_inventory import ReportPackageRefused
from noah.reports.package_snapshot import snapshot_package


def package(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    _write(root / 'value.json', {'safe': True})
    _write(root / 'manifest.json', {'files': _inventory(root)})
    pin = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
    return root, pin


def test_replay_copy_isolated_from_later_source_mutation(tmp_path):
    root, pin = package(tmp_path)
    original = (root / 'value.json').read_bytes()
    with snapshot_package(root, manifest_sha256=pin, limits=LIMITS) as private:
        assert private != root and private.stat().st_mode & 0o777 == 0o700
        (root / 'value.json').write_bytes(b'changed source')
        assert (private / 'value.json').read_bytes() == original
        retained = private
    assert not retained.exists()


@pytest.mark.parametrize('replacement', ['symlink', 'fifo', 'oversized'])
def test_mutation_after_inventory_never_reaches_semantic_decoder(tmp_path, monkeypatch, replacement):
    import noah.reports.package_snapshot as module
    root, pin = package(tmp_path)
    real = module.validate_package_inventory
    first = [True]
    def mutate(*args, **kwargs):
        result = real(*args, **kwargs)
        if first[0]:
            first[0] = False
            path = root / 'value.json'
            path.unlink()
            if replacement == 'symlink':
                path.symlink_to(root / 'manifest.json')
            elif replacement == 'fifo':
                os.mkfifo(path)
            else:
                path.write_bytes(b'x' * 1024)
        return result
    monkeypatch.setattr(module, 'validate_package_inventory', mutate)
    with pytest.raises(ReportPackageRefused):
        with snapshot_package(root, manifest_sha256=pin, limits=LIMITS):
            pytest.fail('unsafe source was exposed to decoder')


def test_changed_same_size_bytes_fail_external_digest(tmp_path, monkeypatch):
    import noah.reports.package_snapshot as module
    root, pin = package(tmp_path)
    real = module._copy_member
    def changed(descriptor, target, name, *args):
        if name == 'value.json':
            path = root / name
            path.write_bytes(path.read_bytes().replace(b'true', b'null'))
        return real(descriptor, target, name, *args)
    monkeypatch.setattr(module, '_copy_member', changed)
    with pytest.raises(ReportPackageRefused):
        with snapshot_package(root, manifest_sha256=pin, limits=LIMITS):
            pytest.fail('changed source was accepted')


def test_exact_semantic_member_set_cannot_omit_an_inventoried_file(tmp_path):
    from noah.reports.package_inventory import validate_package_inventory
    root, pin = package(tmp_path)
    assert validate_package_inventory(root, manifest_sha256=pin, limits=LIMITS,
        expected_members={'manifest.json', 'value.json'})['structural_inventory_verified']
    with pytest.raises(ReportPackageRefused, match='omits or adds'):
        validate_package_inventory(root, manifest_sha256=pin, limits=LIMITS,
                                   expected_members={'manifest.json'})
