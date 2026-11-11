"""Explicit prospective amendment; the complete predecessor stays byte-identical.

This module constructs/validates documents only. It never replaces installed
policy files. The release transaction must independently establish the permitted
pre-first-publication transition and preserve its predecessor and receipt.
"""
import hashlib
import json


SCHEMA = 'noah-points-policy/3'
POLICY = 'noah-points-402040-30d/3'
MODE = 'points-402040-v3'
INPUT_SCHEMA = 'noah-points-fixture/2'
RULE = 'same-incarnation-prior-utc-terminal-unsupported/1'
FIELDS = {'schema', 'policy', 'predecessor_json', 'predecessor_sha256', 'effective_day', 'rule'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate policy key')
        result[key] = value
    return result


def validate_document(document):
    if not isinstance(document, dict) or set(document) != FIELDS:
        raise ValueError('unexpected amendment fields')
    if (document['schema'], document['policy'], document['rule']) != (SCHEMA, POLICY, RULE):
        raise ValueError('unsupported amendment')
    text = document['predecessor_json']
    if not isinstance(text, str) or hashlib.sha256(text.encode('utf-8')).hexdigest() != document['predecessor_sha256']:
        raise ValueError('predecessor bytes differ')
    predecessor = json.loads(text, object_pairs_hook=_unique)
    if not isinstance(predecessor, dict) or predecessor.get('schema') != 'noah-points-policy/2':
        raise ValueError('amendment requires complete v2 predecessor')
    from .migration import validate
    m = predecessor['migration']
    validate(m, expected_genesis=m['genesis'], expected_cutoff_hash=m['cutoff_hash'])
    if type(document['effective_day']) is not int or document['effective_day'] != m['cutoff_day'] + 1:
        raise ValueError('amendment must preserve first prospective day')
    return predecessor


def build(predecessor_bytes):
    if type(predecessor_bytes) is not bytes:
        raise ValueError('exact predecessor bytes required')
    text = predecessor_bytes.decode('utf-8')
    previous = json.loads(text, object_pairs_hook=_unique)
    document = dict(schema=SCHEMA, policy=POLICY, predecessor_json=text,
                    predecessor_sha256=hashlib.sha256(predecessor_bytes).hexdigest(),
                    effective_day=previous['migration']['cutoff_day'] + 1, rule=RULE)
    validate_document(document)
    return document


def baseline(document):
    return validate_document(document) if document.get('schema') == SCHEMA else document


def manifest(document, netuid):
    """Canonical mode identity shared by startup and release preparation."""
    from ..core.calculation_record import digest, encoded
    old = validate_document(document)
    m = old['migration']
    return dict(policy=POLICY, scoring_mode=MODE, netuid=netuid,
                sha256=digest(encoded(document)), cutoff_day=m['cutoff_day'],
                cutoff_block=m['cutoff_block'], pricing=old['pricing'])


def check_transition(adapter, document):
    """Read-only pre-first-publication gate; never grants permission to persist."""
    from pathlib import Path
    from ..core.calculation_record import encoded
    from ..core.commit_ledger import CommitLedger
    from .mode import source_mode
    old = validate_document(document)
    root = Path(adapter._submitted_record_dir)
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('symlinked amendment state path')
    installed = root / 'points-policy.json'
    predecessor_bytes = document['predecessor_json'].encode('utf-8')
    configured = root.parent.parent / 'points-policy.json'
    if configured.is_symlink() or not configured.is_file() or configured.read_bytes() != predecessor_bytes:
        raise ValueError('original configured predecessor bytes differ')
    archive = root / 'points-policy-predecessor.json'
    if archive.exists() and (archive.is_symlink() or not archive.is_file() or archive.read_bytes() != predecessor_bytes):
        raise ValueError('retained predecessor differs')
    if installed.exists():
        if not installed.is_file() or installed.is_symlink():
            raise ValueError('unsafe installed policy')
        current = installed.read_bytes()
        if current != encoded(old) and not (current == encoded(document) and archive.is_file() and archive.read_bytes() == predecessor_bytes):
            raise ValueError('amendment transition requires predecessor or exact journaled successor')
    pending = getattr(adapter, '_publish_intent', None)
    if pending is not None and pending.load() is not None:
        raise ValueError('pending publication forbids amendment')
    for name in ('starter-pending.json', 'starter-intent.json'):
        if (root / name).exists():
            raise ValueError('pending starter publication forbids amendment')
    cutoff = old['migration']['cutoff_day']
    for path in root.rglob('*.submitted.json'):
        if path.is_symlink():
            raise ValueError('symlinked submission record')
        record = json.loads(path.read_bytes(), object_pairs_hook=_unique)
        if record.get('schema') != 'noah-submitted-weights/1' or type(record.get('day')) is not int:
            raise ValueError('unknown submission record')
        reference = record.get('calculation_evidence')
        if record['day'] > cutoff or (reference is not None and source_mode(root, reference)[0] != 'legacy'):
            raise ValueError('publication after predecessor cutoff forbids amendment')
    active = root / 'starter-active.json'
    if active.exists():
        record = json.loads(active.read_bytes(), object_pairs_hook=_unique)
        if source_mode(root, record['reference'])[0] != 'legacy':
            raise ValueError('points active publication forbids amendment')
    # Direct-mode deployments normally have no commit ledger; retained sealed
    # commits still count even if their submitted marker is missing.
    ledger_reader = getattr(adapter, '_commit_ledger', None)
    if ledger_reader is None:
        ledger_reader = CommitLedger(root.parent.parent / 'ticket-14/state/commit-ledger.json')
    ledger = ledger_reader.load()
    if ledger and any(day > cutoff for day in ledger):
        raise ValueError('post-cutoff sealed commit forbids amendment')
    from .transition import confirm_legacy_current
    confirm_legacy_current(adapter, root)
    return old


def main():
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predecessor', type=Path, required=True)
    parser.add_argument('--predecessor-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    before = args.predecessor.read_bytes()
    if hashlib.sha256(before).hexdigest() != args.predecessor_sha256:
        raise ValueError('predecessor digest differs')
    from ..core.calculation_record import encoded
    value = encoded(build(before))
    if args.predecessor.read_bytes() != before:
        raise ValueError('predecessor changed')
    with args.output.open('xb') as stream:
        stream.write(value)
    print(json.dumps(dict(status='prepared-only', sha256=hashlib.sha256(value).hexdigest(),
                          predecessor_sha256=args.predecessor_sha256, canonical_writes=False)))


if __name__ == '__main__':
    main()
