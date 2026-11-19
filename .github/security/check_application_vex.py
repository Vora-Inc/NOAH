"""Retain raw audits; apply one independently reviewed, exact-public-tree VEX.

This is a CI decision layer, not a patched-component claim. Policy authorship is
trusted through protected independent PR review, not self-authenticating JSON.
Never regenerate policy bindings in CI. The only inventory exclusion is POLICY.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import time

POLICY = '.github/security/ecdsa-application-vex.json'
ALIASES = frozenset({'PYSEC-2026-1325', 'CVE-2024-23342', 'GHSA-wj6h-64fc-37mp'})
PACKAGES = ('ecdsa==0.19.2', 'substrate-interface==1.7.11', 'bittensor-wallet==4.1.1')
LOCKS = ('constraints.txt', 'report-requirements-lock.txt', 'report-wallet-constraints.txt')
SHA = re.compile(r'[0-9a-f]{64}')


class Refused(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def pairs(items):
    result = {}
    for k, v in items:
        require(k not in result, 'duplicate JSON key')
        result[k] = v
    return result


def load(path):
    with Path(path).open('rb') as f:
        data = f.read(16 * 1024 * 1024 + 1)
    require(len(data) <= 16 * 1024 * 1024, 'JSON exceeds bound')
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite JSON')))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def safe_path(name):
    require(type(name) is str and bool(name) and '\\' not in name
            and not PurePosixPath(name).is_absolute()
            and all(p not in ('', '.', '..') for p in name.split('/')), 'unsafe inventory path')


def tracked(root):
    require(subprocess.check_output(['git', 'rev-parse', '--show-prefix'], cwd=root).strip() == b'',
            'public checkout must be Git root')
    records = subprocess.check_output(['git', 'ls-files', '-s', '-z'], cwd=root).split(b'\0')
    result = {}
    for record in filter(None, records):
        header, rawname = record.split(b'\t', 1)
        mode, _, stage = header.decode().split()
        name = rawname.decode('utf-8'); safe_path(name)
        require(stage == '0' and mode in ('100644', '100755') and name not in result,
                'unmerged, duplicate, symlink or nonregular tracked entry')
        result[name] = mode
    require(result and POLICY in result, 'missing tracked policy')
    return result


def verify_tree(root, policy, names=None):
    names = tracked(root) if names is None else names
    expected = policy['files']
    require(type(expected) is dict and POLICY not in expected and set(names) == set(expected) | {POLICY},
            'complete public file inventory changed')
    for name, binding in expected.items():
        safe_path(name)
        require(type(binding) is dict and set(binding) == {'sha256', 'mode'}
                and type(binding['sha256']) is str and SHA.fullmatch(binding['sha256'])
                and binding['mode'] in ('100644', '100755'), 'invalid file binding')
        p = root / name
        require(all(not q.is_symlink() for q in (p, *p.parents) if q != root.parent), 'inventory symlink')
        s = p.stat()
        mode = '100755' if s.st_mode & 0o111 else '100644'
        require(stat.S_ISREG(s.st_mode) and names[name] == binding['mode'] == mode
                and sha(p) == binding['sha256'], 'public source or mode drift: ' + name)
    require(not (root / POLICY).is_symlink(), 'policy symlink')
    for name, digest in policy['locks'].items():
        require(name in LOCKS and sha(root / name) == digest, 'lock drift')
    require(set(policy['locks']) == set(LOCKS), 'incomplete lock binding')


def validate_policy(p):
    require(type(p) is dict and set(p) == {'schema', 'status', 'justification', 'component',
            'subject', 'review', 'control_review', 'files', 'locks', 'packages', 'wheels', 'advisory_sha256'}, 'invalid policy fields')
    require(p['schema'] == 'noah.application-vex/1' and p['status'] == 'not_affected'
            and p['justification'] == 'vulnerable_code_not_in_execute_path', 'unsupported disposition')
    require(p['component'] == {'name': 'ecdsa', 'version': '0.19.2', 'affected': True,
            'advisory': 'PYSEC-2026-1325', 'aliases': sorted(ALIASES)}, 'component scope differs')
    require(p['subject'] == {'platform': 'linux-x86_64-cpython-3.11',
            'scope': 'supported-public-noah-entrypoints', 'inventory_exclusion': POLICY}, 'subject differs')
    for review in (p['review'], p['control_review']):
        require(type(review) is dict and set(review) == {'owner', 'date', 'evidence_sha256', 'status'}
                and type(review['owner']) is str and bool(review['owner'].strip())
                and type(review['date']) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', review['date'])
                and type(review['evidence_sha256']) is str and SHA.fullmatch(review['evidence_sha256'])
                and review['status'] == 'reviewed', 'independent review missing')
        from datetime import date, datetime, timezone
        require(date.fromisoformat(review['date']) <= datetime.now(timezone.utc).date(), 'review date is in future')
    require(type(p['advisory_sha256']) is str and SHA.fullmatch(p['advisory_sha256']), 'invalid advisory binding')
    require(set(p['packages']) == {'public', 'reports'}, 'missing audit input set')
    for rows in p['packages'].values():
        require(type(rows) is dict and rows and all(type(k) is str and type(v) is str for k, v in rows.items()),
                'invalid package inventory')
    require(p['packages']['reports'].get('ecdsa') == '0.19.2', 'affected package binding missing')


def advisory_digest(v):
    # Exact description and fixes; alias ordering alone is not a semantic change.
    canonical = dict(v, aliases=sorted(v['aliases']))
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def assess(raw, exit_code, expected, allow_ecdsa, advisory_sha256):
    require(type(raw) is dict and set(raw) == {'dependencies', 'fixes'}
            and raw['fixes'] == [] and type(raw['dependencies']) is list, 'invalid scanner output')
    seen = {}; findings = []
    for row in raw['dependencies']:
        require(type(row) is dict and set(row) == {'name', 'version', 'vulns'}
                and type(row['name']) is str and row['name'] not in seen
                and type(row['version']) is str and type(row['vulns']) is list,
                'skipped, duplicate or malformed dependency')
        seen[row['name']] = row['version']
        for v in row['vulns']:
            require(type(v) is dict and set(v) == {'id', 'aliases', 'fix_versions', 'description'}
                    and type(v['aliases']) is list and all(type(a) is str for a in v['aliases'])
                    and type(v['description']) is str and bool(v['description']), 'malformed advisory')
            require(allow_ecdsa and row['name'] == 'ecdsa' and row['version'] == '0.19.2'
                    and type(v['id']) is str and v['id'] in ALIASES
                    and {v['id'], *v['aliases']} == ALIASES and v['fix_versions'] == [],
                    'new or changed advisory requires review')
            require(advisory_digest(v) == advisory_sha256, 'advisory content changed; new review required')
            findings.append(v)
    require(seen == expected, 'scanner package inventory differs')
    require(type(exit_code) is int and exit_code == (1 if findings else 0), 'scanner failed or inconsistent exit')
    return {'raw_exit': exit_code, 'component_findings': len(findings),
            'application_status': 'not_affected' if findings else 'no_findings',
            'component_still_affected': bool(findings)}


def capture(argv, cwd, output, name, timeout=300):
    start = time.time(); error = None; code = None
    with (output / (name + '.stdout')).open('wb') as out, (output / (name + '.stderr')).open('wb') as err:
        try:
            code = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                  timeout=timeout, check=False).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = type(exc).__name__
    record = {'argv': argv, 'started_unix': start, 'finished_unix': time.time(),
              'exit_code': code, 'execution_error': error}
    write(output / (name + '.execution.json'), record)
    return record


def verify_wheels(root, p, directory):
    require(platform.system() == 'Linux' and platform.machine() == 'x86_64'
            and sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 11),
            'native platform outside reviewed scope')
    expected = p['wheels']
    require(type(expected) is dict and len(expected) == 3 and
            set(expected) == {'bittensor_wallet-4.1.1-cp311-cp311-manylinux_2_28_x86_64.whl',
                'ecdsa-0.19.2-py2.py3-none-any.whl', 'substrate_interface-1.7.11-py3-none-any.whl'},
            'wheel subject differs')
    require({f.name for f in directory.iterdir()} == set(expected), 'downloaded wheel set differs')
    lock_text = '\n'.join((root / n).read_text() for n in LOCKS[1:])
    for name, digest in expected.items():
        require(type(digest) is str and SHA.fullmatch(digest) and '--hash=sha256:' + digest in lock_text
                and sha(directory / name) == digest, 'wheel/lock hash differs')


def run_python(root, output):
    scans = {}
    # Always collect both raw scans, even if the first fails. No ignore switches.
    for name, requirements in [('public', LOCKS[:1]), ('reports', LOCKS[1:])]:
        argv = [sys.executable, '-I', '-m', 'pip_audit', '--disable-pip', '--no-deps', '--format', 'json']
        for requirement in requirements:
            argv += ['--requirement', requirement]
        scans[name] = capture(argv, root, output, name)
    try:
        require(importlib.metadata.version('pip-audit') == '2.10.1', 'scanner version differs')
        policy = load(root / POLICY); validate_policy(policy); verify_tree(root, policy)
        results = {}
        for name, record in scans.items():
            require(record['execution_error'] is None, 'scanner execution failed')
            results[name] = assess(load(output / (name + '.stdout')), record['exit_code'],
                                   policy['packages'][name], name == 'reports', policy['advisory_sha256'])
        wheels = output / 'reviewed-wheels'; wheels.mkdir()
        download = capture([sys.executable, '-I', '-m', 'pip', 'download', '--only-binary=:all:',
                            '--no-deps', '--dest', str(wheels), *PACKAGES], root, output, 'wheels')
        require(download['exit_code'] == 0 and download['execution_error'] is None, 'wheel download failed')
        verify_wheels(root, policy, wheels)
        write(output / 'application-policy.json', {'status': 'passed', 'scans': results,
              'policy_sha256': sha(root / POLICY), 'scope': policy['subject'],
              'note': 'Raw component findings preserved; not an upstream fix or release authorization.'})
        print('Application policy PASS; inspect raw scans: ecdsa component remains affected.')
        return 0
    except Exception as exc:
        write(output / 'application-policy.json', {'status': 'refused', 'reason': str(exc), 'type': type(exc).__name__})
        print('Application policy REFUSED:', str(exc), file=sys.stderr)
        return 1


def run_node(root, output):
    r = capture(['npm', 'audit', '--package-lock-only', '--ignore-scripts', '--json'],
                root / 'verification/noah-ts', output, 'node')
    try:
        raw = load(output / 'node.stdout')
        require(type(r['exit_code']) is int and r['exit_code'] == 0 and r['execution_error'] is None,
                'Node scanner execution failure')
        require(type(raw) is dict and set(raw) == {'auditReportVersion', 'vulnerabilities', 'metadata'}
                and type(raw['auditReportVersion']) is int and raw['auditReportVersion'] == 2
                and type(raw['vulnerabilities']) is dict and not raw['vulnerabilities']
                and type(raw['metadata']) is dict
                and set(raw['metadata']) == {'vulnerabilities', 'dependencies'},
                'Node scanner malformed report or findings')
        counts = raw['metadata']['vulnerabilities']
        require(type(counts) is dict and set(counts) == {'info', 'low', 'moderate', 'high', 'critical', 'total'}
                and all(type(v) is int and v >= 0 for v in counts.values())
                and all(v == 0 for v in counts.values()), 'Node scanner incomplete counters or findings')
        dependencies = raw['metadata']['dependencies']
        require(type(dependencies) is dict
                and set(dependencies) == {'prod', 'dev', 'optional', 'peer', 'peerOptional', 'total'}
                and all(type(v) is int and v >= 0 for v in dependencies.values())
                and dependencies['total'] > 0, 'Node scanner incomplete dependency accounting')
        write(output / 'node-policy.json', {'status': 'passed', 'raw_exit': 0})
        return 0
    except Exception as exc:
        write(output / 'node-policy.json', {'status': 'refused', 'reason': str(exc)})
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('python', 'node'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(); root = Path.cwd().resolve(); output = args.output.resolve()
    require(not output.is_relative_to(root), 'audit evidence must stay outside shipping tree')
    output.mkdir(parents=True, exist_ok=True)
    return run_python(root, output) if args.command == 'python' else run_node(root, output)


if __name__ == '__main__':
    raise SystemExit(main())
