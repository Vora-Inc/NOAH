"""Bounded structural checks for future retained report packages.

This does not authenticate chain facts, resolve semantic references, reconstruct
verdicts or authorize publication. The provider must do those checks separately.
No existing backup entry point calls this primitive. Limits are caller supplied;
ordinary score schemas and their limits are unchanged.
"""
from dataclasses import dataclass, fields
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zlib


class ReportPackageRefused(ValueError):
    pass


@dataclass(frozen=True)
class ReportPackageLimits:
    manifest_bytes: int
    member_bytes: int
    expanded_member_bytes: int
    total_bytes: int
    total_expanded_bytes: int
    members: int
    path_depth: int
    json_depth: int
    line_bytes: int

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ReportPackageRefused('positive integer package limits required')
        if self.json_depth > 128 or self.path_depth > 32:
            raise ReportPackageRefused('package depth limits exceed parser bounds')


ENTRY_FIELDS = {'path', 'kind', 'encoding', 'sha256', 'bytes', 'expanded_sha256', 'expanded_bytes'}
JSONL_KINDS = {'rpc_responses', 'history_segment', 'receipts'}
KIND_ENCODINGS = {'json': 'identity', 'manifest': 'identity', 'metadata': 'identity',
                  **{kind: 'gzip' for kind in JSONL_KINDS}}
HASH = re.compile(r'[0-9a-f]{64}')
CHUNK = 64 * 1024


def _refuse():
    raise ReportPackageRefused('report package inventory, bytes or bounded format differs')


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _json(data, depth):
    # Bound nesting before entering the recursive JSON decoder. Strings may
    # contain braces; escapes and quoted content do not affect the depth.
    level, quoted, escaped = 0, False, False
    for char in data.decode('utf-8'):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            level += 1
            if level > depth:
                _refuse()
        elif char in ']}':
            level -= 1
    return json.loads(data, object_pairs_hook=_pairs, parse_float=Decimal,
                      parse_constant=lambda _: _refuse())


def _path(value, limits):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _refuse()
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or any(p in ('.', '..') for p in path.parts)
            or len(path.parts) > limits.path_depth):
        _refuse()
    return path.parts


def _root_fd(root):
    absolute = Path(root).absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if part in ('.', '..'):
                _refuse()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _tree(root_fd, limits, expected):
    found = {}
    directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents if str(parent) != '.'}
    def visit(fd, prefix):
        with os.scandir(fd) as iterator:
            for item in iterator:
                leaf = item.name
                name = prefix + leaf
                _path(name, limits)
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if name not in directories:
                        _refuse()
                    child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, name + '/')
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if name not in expected or info.st_size != expected[name] or len(found) >= limits.members + 1:
                        _refuse()
                    found[name] = _signature(info)
                else:
                    _refuse()
    visit(root_fd, '')
    if set(found) != set(expected):
        _refuse()
    return found


def _check_member(root_fd, entry, limits, signature):
    with os.fdopen(_open(root_fd, _path(entry['path'], limits)), 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _signature(before) != signature:
            _refuse()
        raw_hash = hashlib.sha256()
        count = 0
        while chunk := source.read(CHUNK):
            count += len(chunk)
            if count > entry['bytes']:
                _refuse()
            raw_hash.update(chunk)
        if count != entry['bytes'] or raw_hash.hexdigest() != entry['sha256']:
            _refuse()
        source.seek(0)
        decoded = gzip.GzipFile(fileobj=source, mode='rb') if entry['encoding'] == 'gzip' else source
        digest, count = hashlib.sha256(), 0
        try:
            if entry['kind'] in JSONL_KINDS:
                while line := decoded.readline(limits.line_bytes + 1):
                    count += len(line)
                    if len(line) > limits.line_bytes or count > entry['expanded_bytes']:
                        _refuse()
                    digest.update(line)
                    if not isinstance(_json(line, limits.json_depth), dict):
                        _refuse()
            else:
                chunks = []
                while chunk := decoded.read(min(CHUNK, entry['expanded_bytes'] - count + 1)):
                    count += len(chunk)
                    if count > entry['expanded_bytes']:
                        _refuse()
                    digest.update(chunk)
                    chunks.append(chunk)
                data = b''.join(chunks)
                if entry['kind'] in ('json', 'manifest'):
                    _json(data, limits.json_depth)
                elif (not data.startswith(b'0x') or len(data) <= 2 or len(data) % 2
                      or any(c not in b'0123456789abcdefABCDEF' for c in data[2:])):
                    _refuse()
        finally:
            if decoded is not source:
                decoded.close()
        if (count != entry['expanded_bytes'] or digest.hexdigest() != entry['expanded_sha256']
                or _signature(os.fstat(source.fileno())) != signature):
            _refuse()


def validate_package_inventory(root, *, manifest_sha256, limits, expected_members=None):
    """Verify an externally pinned root manifest and its complete byte inventory.

    The root manifest is excluded from its own files list; nested manifests use
    kind=manifest and are ordinary inventoried members. Aggregate budgets include
    the root manifest. expected_members optionally pins the caller's complete
    mapping in this same read. No saved status, verdict or reference is trusted here.
    """
    if not isinstance(limits, ReportPackageLimits) or not isinstance(manifest_sha256, str) or not HASH.fullmatch(manifest_sha256):
        _refuse()
    try:
        fd = _root_fd(root)
        try:
            with os.fdopen(_open(fd, ('manifest.json',)), 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limits.manifest_bytes:
                    _refuse()
                raw = handle.read(limits.manifest_bytes + 1)
                if len(raw) != info.st_size or _signature(info) != _signature(os.fstat(handle.fileno())):
                    _refuse()
            if hashlib.sha256(raw).hexdigest() != manifest_sha256:
                _refuse()
            manifest = _json(raw, limits.json_depth)
            entries = manifest.get('files') if isinstance(manifest, dict) else None
            if not isinstance(entries, list) or not entries or len(entries) > limits.members:
                _refuse()
            expected = {'manifest.json': len(raw)}
            total = expanded = len(raw)
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
                    _refuse()
                name = entry['path']
                _path(name, limits)
                if (name in expected or KIND_ENCODINGS.get(entry['kind']) != entry['encoding']
                        or any(not isinstance(entry[k], str) or not HASH.fullmatch(entry[k]) for k in ('sha256', 'expanded_sha256'))
                        or type(entry['bytes']) is not int or not 0 < entry['bytes'] <= limits.member_bytes
                        or type(entry['expanded_bytes']) is not int or not 0 <= entry['expanded_bytes'] <= limits.expanded_member_bytes):
                    _refuse()
                if entry['encoding'] == 'identity' and (entry['sha256'] != entry['expanded_sha256'] or entry['bytes'] != entry['expanded_bytes']):
                    _refuse()
                expected[name] = entry['bytes']
                total += entry['bytes']
                expanded += entry['expanded_bytes']
            if expected_members is not None and (type(expected_members) not in (set, frozenset)
                    or expected_members != set(expected)):
                raise ReportPackageRefused('mapping omits or adds report package members')
            if total > limits.total_bytes or expanded > limits.total_expanded_bytes:
                _refuse()
            before = _tree(fd, limits, expected)
            if before['manifest.json'] != _signature(info):
                _refuse()
            for entry in entries:
                _check_member(fd, entry, limits, before[entry['path']])
            if before != _tree(fd, limits, expected):
                _refuse()
            current_fd = _root_fd(root)
            try:
                a, b = os.fstat(fd), os.fstat(current_fd)
                if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                    _refuse()
            finally:
                os.close(current_fd)
            return {'manifest_sha256': manifest_sha256, 'members': len(entries),
                    'stored_bytes': total, 'expanded_bytes': expanded,
                    'structural_inventory_verified': True}
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, KeyError, EOFError, RecursionError, zlib.error) as error:
        if isinstance(error, ReportPackageRefused):
            raise
        raise ReportPackageRefused('report package bytes, paths or format are invalid') from None
