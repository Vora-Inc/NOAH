"""Offline operator preparation for one producer handoff; never installs it.

Run with the successor validator's isolated interpreter. Both authenticated
release trees and archives, and independently generated successor R0, are
required. Only the explicit output is written, after every check succeeds.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile

from .calculation_record import artifact, digest, durable_write, encoded, verify
from .report_publication import require, semantic_digest
from . import report_source_handoff as h


def file_hash(path, limit=1024*1024*1024, *, allow_empty=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and (0 if allow_empty else 1) <= before.st_size <= limit, 'handoff artifact file refused')
        result = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                result.update(chunk)
        signature = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        require(signature(before) == signature(os.fstat(fd)) == signature(Path(path).lstat()),
                'handoff artifact changed while reading')
        return result.hexdigest()
    finally:
        os.close(fd)


def protected_tree(path):
    for parent in (Path(path), *Path(path).parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "handoff executable tree is not protected")


def release(spec):
    """Authenticate full installed payload against the pinned actual archive."""
    require(type(spec) is dict and set(spec) == {'root', 'archive', 'archive_sha256', 'manifest_sha256'},
            'handoff preparation release specification differs')
    root = Path(spec['root'])
    require(root.is_absolute() and not root.is_symlink(), 'handoff release root differs')
    protected_tree(root)
    archive = Path(spec['archive'])
    require(file_hash(archive) == spec['archive_sha256'], 'handoff archive hash differs')
    raw = h.read_bytes(root / 'payload/manifest.json')
    require(digest(raw) == spec['manifest_sha256'], 'handoff payload manifest differs')
    manifest = json.loads(raw)
    seen = set();expanded = 0
    with tarfile.open(archive, 'r:gz') as bundle:
        for member in bundle:
            name = member.name
            expanded += member.size
            require(len(seen) < 20000 and 0 <= member.size <= 1024**3 and expanded <= 3*1024**3
                    and not member.mode & 0o7000, 'handoff archive exceeds maintained bounds')
            require(member.isfile() and not name.startswith('/') and '..' not in name.split('/')
                    and name not in seen, 'handoff archive member refused')
            seen.add(name)
            # The maintained archive is the payload directory itself.
            data = bundle.extractfile(member)
            sha = hashlib.sha256()
            for chunk in iter(lambda: data.read(1024*1024), b''):
                sha.update(chunk)
            # The archive and manifest are already SHA-pinned above. Empty installed
            # members are admitted only when BOTH declare this exact zero-byte member.
            declared = manifest['files'].get(name, {})
            empty_member = (name != 'manifest.json' and member.size == 0
                            and type(declared.get('size')) is int and declared['size'] == 0
                            and declared.get('mode') == member.mode
                            and declared.get('sha256') == hashlib.sha256(b'').hexdigest()
                            and sha.hexdigest() == declared['sha256'])
            # bundle.extract normalizes the non-executable envelope manifest to0644.
            # Payload member modes still match their manifest/archive declarations exactly.
            installed_mode = member.mode
            if name == 'manifest.json':
                require(member.mode in (0o600, 0o644), 'handoff archive manifest mode differs')
                installed_mode = 0o644
            target = root / 'payload' / name
            protected_tree(target.parent)
            info = target.lstat()
            require(info.st_uid == 0 and not info.st_mode & 0o022
                    and info.st_size == member.size and stat.S_IMODE(info.st_mode) == installed_mode
                    and file_hash(target, allow_empty=empty_member) == sha.hexdigest(), 'handoff installed artifact differs')
            if name == 'manifest.json':
                require(sha.hexdigest() == spec['manifest_sha256'], 'handoff archive manifest differs')
            else:
                require(name in manifest['files'] and manifest['files'][name]['sha256'] == sha.hexdigest()
                        and manifest['files'][name]['size'] == member.size
                        and manifest['files'][name]['mode'] == member.mode,
                        'handoff manifest member differs')
    require(seen == set(manifest['files']) | {'manifest.json'}, 'handoff archive member set differs')
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    require(root.name == manifest['commit'][:12] + '-' + digest(canonical)[:16],
            'handoff release ID differs')
    # Managed venv copies must match the authenticated executable and library.
    for name, member in [('bin/python', 'python/bin/python3.11'),
                         ('lib/libpython3.11.so.1.0', 'python/lib/libpython3.11.so.1.0')]:
        protected_tree((root / 'venv' / name).parent)
        installed = (root / 'venv' / name).lstat()
        require(installed.st_uid == 0 and not installed.st_mode & 0o022, 'handoff interpreter is not protected')
        require(file_hash(root / 'venv' / name) == manifest['files'][member]['sha256'],
                'handoff core interpreter differs')
    return root, manifest


def source_prefix(manifest):
    """The authenticated archive declares one source package, not a host layout."""
    marker = ('noah', 'core', 'calculation_record.py')
    roots = []
    for name in manifest['files']:
        require(type(name) is str and name == Path(name).as_posix()
                and not Path(name).is_absolute() and '..' not in Path(name).parts
                and '\\' not in name and all(ord(c) >= 32 for c in name),
                'handoff manifest source path refused')
        parts = Path(name).parts
        if parts[-3:] == marker:
            require(len(parts) > 3 and parts[0] == 'source', 'handoff package root refused')
            roots.append('/'.join(parts[:-3]) + '/')
    require(len(roots) == 1, 'handoff requires one authenticated source package root')
    return roots[0]


def source_files(manifest):
    prefix = source_prefix(manifest)
    result = {}
    for name, info in manifest['files'].items():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):];parts = Path(rel).parts
        if 'tests' in parts or '__pycache__' in parts:
            continue
        if ((rel.startswith(('noah/', 'neurons/')) and rel.endswith('.py'))
                or (rel.startswith('noah/starter/') and rel.endswith('.mjs'))
                or (rel.startswith('verification/noah-ts/src/') and rel.endswith('.ts'))):
            result[rel] = info['sha256']
    return result


def replay(root, fixture, manifest):
    """Four individual engine outputs are retained, not an asserted pass flag."""
    source = root / 'payload' / source_prefix(manifest)
    code = """import json,sys
sys.path.insert(0,sys.argv[1])
from noah.core.calculation_record import source_identity,encoded
from noah.points.engine import evaluate
from noah.points.run import typescript_text
raw=json.load(open(sys.argv[2]))
a=evaluate(raw);b=json.loads(typescript_text(raw))
assert a==b
print(json.dumps({'candidate':source_identity(),'python':encoded(a).hex(),'typescript':encoded(b).hex()}))
"""
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONOPTIMIZE')}
    env['PATH'] = str(root / 'payload/node/bin') + ':/usr/bin:/bin'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    with tempfile.TemporaryDirectory(prefix='noah-handoff-twins-') as tmp:
        path = Path(tmp) / 'fixture.json';path.write_bytes(encoded(fixture))
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run([str(root / 'venv/bin/python'), '-I', '-B', '-c', code,
                                     str(source), str(path)], env=env, stdout=output, stderr=errors,
                                    timeout=180, check=False)
            require(result.returncode == 0, 'handoff isolated twins refused')
            output.seek(0);raw = output.read(64*1024*1024+1)
            require(len(raw) <= 64*1024*1024, 'handoff twins output exceeds bound')
    result = json.loads(raw)
    require(set(result) == {'candidate', 'python', 'typescript'}, 'handoff replay fields differ')
    h.identity(result['candidate'])
    return result


def prepare(scores, request_path, successor, old_spec, new_spec, approval, checkpoint):
    root = Path(scores)
    request_raw = h.read_bytes(request_path)
    request = json.loads(request_raw)
    frozen = request['calculation']['reference']
    original, old_context, new_context, financial = h.raw_pair(root, successor, frozen)
    # Preparation is not authorization to bypass an uncertain send or completed day.
    day = original['day']
    require(Path(request_path).absolute() == (root / 'report-state' / f'day-{day}.request.json').absolute(),
            'handoff preparation requires the canonical frozen request path')
    for path in (root / f'day-{day}.submitted.json', root / f'day-{day}.confirmed.json', root / f'day-{day}.accounted.json',
                 root / 'publish-intent.json', root.parent / 'publish-intent.json',
                 root.parent.parent / 'publish-intent.json', root.parent.parent / 'publish-intent.private.json'):
        require(not path.exists() and not path.is_symlink(), 'handoff has publication or accounting evidence')
    fixture = artifact(root, original, 'fixture')
    expected = (root / original['artifacts']['agreed_score']['path']).read_bytes()
    releases = [];results = {}
    for label, spec, context in [('old', old_spec, old_context), ('new', new_spec, new_context)]:
        installed, manifest = release(spec)
        result = replay(installed, fixture, manifest)
        require(result['candidate'] == context['candidate']
                and result['candidate']['files'] == source_files(manifest),
                'handoff replay producer differs from retained context or authenticated artifact')
        for engine in ('python', 'typescript'):
            raw = bytes.fromhex(result[engine])
            require(raw == expected, 'handoff engine changes original financial result')
            results[label+'_'+engine] = digest(raw)
        releases.append({'release_id': installed.name, 'commit': manifest['commit'], 'archive_sha256': spec['archive_sha256'],
                         'manifest_sha256': spec['manifest_sha256'], 'candidate': result['candidate']})
    value = dict(schema=h.SCHEMA, day=day, genesis_hash=original['genesis_hash'], netuid=original['netuid'],
                 request_sha256=digest(request_raw), request_semantic_sha256=semantic_digest(request),
                 source=frozen, successor=successor, from_release=releases[0], to_release=releases[1],
                 financial_sha256=financial, replay=results, approval=approval, checkpoint_sha256=checkpoint)
    h.verify_graph(root, value, request_raw)
    require(h.read_bytes(request_path) == request_raw, 'handoff request changed during preparation')
    # Repeat the complete financial graph after both bounded subprocess runs.
    h.verify_graph(root, value, request_raw)
    return value


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('scores', 'request', 'successor-reference', 'old-release', 'new-release', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--approval', required=True)
    parser.add_argument('--checkpoint-sha256', required=True)
    options = parser.parse_args(argv)
    require(not options.output.exists() and not options.output.is_symlink(), 'handoff output already exists')
    load = lambda path: json.loads(h.read_bytes(path))
    value = prepare(options.scores, options.request, load(options.successor_reference),
                    load(options.old_release), load(options.new_release), options.approval, options.checkpoint_sha256)
    durable_write(options.output, encoded(value))
    print(json.dumps({'status':'prepared_not_authorized', 'sha256':digest(encoded(value)), 'day':value['day']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
