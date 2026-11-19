"""Offline export preparation and immutable PRIVATE provenance receipts.

The dispatcher owns parsing and printing: call add_arguments(parser), then emit
execute(args). No endpoint, signing, website index or upload operation exists.
Run in the report CLI worker process; the network guard is process-local.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import socket
import stat
import tempfile

from . import public_export as export

RECEIPT_SCHEMA = 'noah.report-public-export-receipt/1'
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 32768


def add_arguments(parser):
    parser.add_argument('--request', type=Path, required=True, help='frozen private adjudication request JSON')
    parser.add_argument('--package', type=Path, required=True, help='retained private paired-reader package')
    parser.add_argument('--manifest-sha256', required=True, help='externally pinned package manifest SHA-256')
    parser.add_argument('--round-sha256', required=True, help='selected completed round digest')
    parser.add_argument('--activation-receipt', type=Path, required=True, help='private operator approval receipt')
    parser.add_argument('--activation-receipt-sha256', required=True, help='externally pinned approval receipt SHA-256')
    parser.add_argument('--published-at', required=True, help='explicit UTC timestamp YYYY-MM-DDTHH:MM:SSZ; retain for retries')
    parser.add_argument('--supersedes', help='explicit previous report ID for a correction')
    parser.add_argument('--out', type=Path, required=True, help='public JSON output directory; does not upload')
    parser.add_argument('--receipt', type=Path, required=True, help='new private export receipt file outside public output/package')
    return parser


@contextmanager
def _offline():
    def blocked(*_args, **_kwargs):
        raise export.PublicExportError('report public export: offline network access refused')
    targets = ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
               (socket, 'create_connection'), (socket, 'getaddrinfo'),
               (socket, 'gethostbyname'), (socket, 'gethostbyname_ex'), (socket, 'gethostbyaddr'))
    saved = [(owner, name, getattr(owner, name)) for owner, name in targets]
    try:
        for owner, name, _ in saved:
            setattr(owner, name, blocked)
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)


def _inside(path, parent):
    return path == parent or parent in path.parents


def _paths(args):
    public = Path(args.out)
    receipt = Path(args.receipt)
    export._require(not public.is_symlink() and not receipt.is_symlink(), 'output or receipt symlink')
    public, receipt, package = public.resolve(), receipt.resolve(), Path(args.package).resolve()
    export._require(not _inside(public, package) and not _inside(package, public),
                    'public output and private package must be separate trees')
    for private in (receipt, Path(args.request).resolve(), Path(args.activation_receipt).resolve()):
        export._require(not _inside(private, public), 'private input/receipt inside public output')
    export._require(not _inside(receipt, package), 'export receipt must not alter pinned package')
    export._require(receipt not in (Path(args.request).resolve(), Path(args.activation_receipt).resolve()),
                    'export receipt overlaps input')
    return public, receipt


def _sources():
    # Exact installed source-file identities, independent of Git availability.
    # These identify reviewed artifacts, not automatic live activation proof.
    paths = {'public_export.py': Path(export.__file__), 'public_export_cli.py': Path(__file__)}
    return {name: export._sha(export._read_regular(path, MAX_SOURCE_BYTES))
            for name, path in sorted(paths.items())}


@contextmanager
def _receipt_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(directory / '.public-export-receipt.lock', flags, 0o600)
    try:
        info = os.fstat(descriptor)
        export._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'private receipt lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _retain_receipt(path, raw):
    if path.exists() or path.is_symlink():
        export._require(export._read_regular(path, MAX_RECEIPT_BYTES) == raw,
                        'existing private export receipt differs; use a new receipt path')
        export._require(path.stat().st_mode & 0o077 == 0, 'private receipt permissions')
        return False
    descriptor, temporary = tempfile.mkstemp(prefix='.export-receipt-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def execute(args):
    """Verify offline, retain a private intent, then prepare immutable public JSON.

    The immutable receipt is VERIFIED_EXPORT_INTENT, not a publication receipt.
    It is deliberately written before public output: interruption may leave an
    intent without its artifact. Retry the exact command to finish. PREPARED is
    returned only after both files exist; nothing is uploaded or activated.
    Source changes, corrections or changed timestamps need a new receipt path.
    """
    export._require(type(args.published_at) is str and bool(args.published_at),
                    'explicit publication timestamp required')
    export._utc_ms(args.published_at)
    public, receipt_path = _paths(args)
    request_raw = export._read_regular(args.request, MAX_REQUEST_BYTES)
    request = export._strict_json(request_raw, MAX_REQUEST_BYTES)
    source_files = _sources()
    with _receipt_lock(receipt_path.parent), _offline():
        # Do not pass a verifier override: production always uses the default
        # authenticated adjudicator and its private bounded replay snapshot.
        result = export.export_public_round(args.package, request, args.manifest_sha256,
            round_sha256=args.round_sha256, activation_receipt=args.activation_receipt,
            activation_receipt_sha256=args.activation_receipt_sha256,
            published_at=args.published_at, supersedes=args.supersedes, out_dir=None)
        export._require(source_files == _sources(), 'exporter source files changed during verification')
        receipt = {
            'schema': RECEIPT_SCHEMA, 'state': 'VERIFIED_EXPORT_INTENT',
            'request_file_sha256': export._sha(request_raw), 'request_sha256': export._digest(request),
            'policy_sha256': request['policy_sha256'], 'round_sha256': args.round_sha256,
            'manifest_sha256': args.manifest_sha256,
            'activation_receipt_sha256': args.activation_receipt_sha256,
            'exporter_source_files': source_files, 'exporter_source_sha256': export._digest(source_files),
            'published_at': args.published_at, 'report_id': result['report']['report_id'],
            'supersedes': result['report']['supersedes'], 'public_filename': result['filename'],
            'public_file_sha256': export._sha(result['json_bytes']), 'public_file_bytes': len(result['json_bytes']),
        }
        raw = export.canonical_artifact_bytes(receipt)
        export._require(len(raw) <= MAX_RECEIPT_BYTES, 'private receipt size')
        receipt_created = _retain_receipt(receipt_path, raw)
        # Reuse the same immutable bytes just verified, never recalculate with
        # a new clock, activation file or second set of changing source inputs.
        path, created = export._write_export(public, args.package, result['report'], result['json_bytes'], now_ms=None)
    return {'schema': 'noah.report-public-export-result/1', 'status': 'PREPARED',
        'report_id': result['report']['report_id'], 'public_file': str(path),
        'public_file_sha256': receipt['public_file_sha256'], 'public_file_created': created,
        'private_receipt': str(receipt_path), 'private_receipt_sha256': export._sha(raw),
        'private_receipt_created': receipt_created, 'publication_performed': False}
