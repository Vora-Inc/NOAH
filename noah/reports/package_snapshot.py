"""Copy externally pinned bytes into private immutable-by-convention replay input.

The caller-owned source may change. Semantic decoders only see this newly created
0700 tree after bounded, no-follow copies match every externally pinned digest.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from .common import ReportError

from .package_inventory import (
    CHUNK, ReportPackageRefused, _open, _path, _root_fd, _signature,
    validate_package_inventory,
)


def _copy_member(root_fd, target, name, expected_size, expected_hash, limits):
    descriptor = _open(root_fd, _path(name, limits))
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != expected_size:
            raise ReportPackageRefused('report source changed before snapshot')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest, count = hashlib.sha256(), 0
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
            while raw := source.read(min(CHUNK, expected_size - count + 1)):
                count += len(raw)
                if count > expected_size:
                    raise ReportPackageRefused('report source grew during snapshot')
                digest.update(raw)
                out.write(raw)
        if (count != expected_size or digest.hexdigest() != expected_hash
                or _signature(os.fstat(source.fileno())) != _signature(before)):
            raise ReportPackageRefused('report source changed during snapshot')


@contextmanager
def snapshot_package(root, *, manifest_sha256, limits):
    validate_package_inventory(root, manifest_sha256=manifest_sha256, limits=limits)
    descriptor = _root_fd(root)
    try:
        with os.fdopen(_open(descriptor, ('manifest.json',)), 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limits.manifest_bytes:
                raise ReportPackageRefused('report manifest changed before snapshot')
            raw = source.read(limits.manifest_bytes + 1)
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ReportPackageRefused('report manifest pin differs')
        manifest = json.loads(raw)
        # The digest pins the same manifest whose fields and aggregate budgets
        # were validated above. No paths from a new/unpinned manifest are used.
        with tempfile.TemporaryDirectory(prefix='noah-report-replay-') as temporary:
            destination = Path(temporary).resolve()
            os.chmod(destination, 0o700)
            _copy_member(descriptor, destination / 'manifest.json', 'manifest.json',
                         len(raw), manifest_sha256, limits)
            for entry in manifest['files']:
                _copy_member(descriptor, destination / entry['path'], entry['path'],
                             entry['bytes'], entry['sha256'], limits)
            validate_package_inventory(destination, manifest_sha256=manifest_sha256, limits=limits)
            yield destination
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, (ReportPackageRefused, ReportError)):
            raise
        raise ReportPackageRefused('report package snapshot refused') from None
    finally:
        os.close(descriptor)
