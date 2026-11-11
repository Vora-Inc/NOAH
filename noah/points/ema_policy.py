"""Separate prospective EMA policy identity, preserving the exact /3 predecessor.

Document preparation is not installation. Runtime field compatibility and a
controlled release transition are separate prerequisites; no old file is
rewritten by this module and no historic receipt is routed by ingestion time.
"""
import hashlib
import json

from . import amendment

SCHEMA = 'noah-points-policy/4'
POLICY = 'noah-points-402040-30d/4'
MODE = 'points-402040-v4'
INPUT_SCHEMA = 'noah-points-fixture/3'
OUTPUT_SCHEMA = 'noah-points-output/3'
RULE = 'parent-block-ema-spot-minimum/1'
EMA_SOURCES = frozenset({'SubnetMovingPrice'})
FIELDS = {'schema', 'policy', 'predecessor_json', 'predecessor_sha256',
          'activation_anchor', 'effective_block', 'rule', 'ema_source'}


def validate_document(document):
    from .migration import hash32
    if type(document) is not dict or set(document) != FIELDS:
        raise ValueError('unexpected EMA policy fields')
    if (document['schema'], document['policy'], document['rule']) != (SCHEMA, POLICY, RULE):
        raise ValueError('unsupported EMA policy identity')
    if document['ema_source'] not in EMA_SOURCES:
        raise ValueError('unsupported immutable EMA source')
    text = document['predecessor_json']
    if type(text) is not str or hashlib.sha256(text.encode()).hexdigest() != document['predecessor_sha256']:
        raise ValueError('EMA predecessor bytes differ')
    predecessor = json.loads(text, object_pairs_hook=amendment._unique)
    baseline = amendment.validate_document(predecessor)
    anchor = document['activation_anchor']
    if (type(anchor) is not dict or set(anchor) != {'block', 'hash'}
            or type(anchor['block']) is not int
            or not baseline['migration']['cutoff_block'] < anchor['block'] < 2**53 - 1
            or not hash32(anchor['hash'])):
        raise ValueError('invalid finalized activation anchor')
    if type(document['effective_block']) is not int or document['effective_block'] != anchor['block'] + 1:
        raise ValueError('EMA starts with the complete block after its anchor')
    return predecessor


def build(predecessor_bytes, activation_anchor, *, ema_source="SubnetMovingPrice"):
    if type(predecessor_bytes) is not bytes:
        raise ValueError('exact amended predecessor bytes required')
    document = dict(schema=SCHEMA, policy=POLICY,
                    predecessor_json=predecessor_bytes.decode('utf-8'),
                    predecessor_sha256=hashlib.sha256(predecessor_bytes).hexdigest(),
                    activation_anchor=dict(activation_anchor),
                    effective_block=activation_anchor['block'] + 1, rule=RULE, ema_source=ema_source)
    validate_document(document)
    return document


def baseline(document):
    return amendment.validate_document(validate_document(document))


def uses_ema(document, receipt_block):
    validate_document(document)
    if type(receipt_block) is not int or receipt_block < 0:
        raise ValueError('invalid receipt block')
    return receipt_block >= document['effective_block']


def manifest(document, netuid):
    from ..core.calculation_record import digest, encoded
    old = baseline(document)
    m = old['migration']
    return dict(policy=POLICY, scoring_mode=MODE, netuid=netuid,
                sha256=digest(encoded(document)), cutoff_day=m['cutoff_day'],
                cutoff_block=m['cutoff_block'], pricing=dict(
                    method=RULE, ema_source=document['ema_source'], activation_anchor=document['activation_anchor'],
                    effective_block=document['effective_block'],
                    predecessor_policy_sha256=document['predecessor_sha256']))


def fixture_boundary(document, netuid):
    from ..core.calculation_record import digest, encoded
    validate_document(document)
    return dict(genesis=baseline(document)['migration']['genesis'], nativeNetuid=netuid, emaSource=document['ema_source'], anchor=document['activation_anchor'], effectiveBlock=document['effective_block'],
                policySha256=digest(encoded(document)),
                predecessorPolicySha256=document['predecessor_sha256'])


def check_transition(adapter, document):
    """Read-only /3→/4 preview; installed release journaling owns every write."""
    from pathlib import Path
    from ..core.calculation_record import encoded
    from ..core.commit_ledger import CommitLedger
    from .transition import confirm_current
    predecessor=validate_document(document)
    root=Path(adapter._submitted_record_dir)
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('symlinked EMA transition state')
    prior_bytes=document['predecessor_json'].encode()
    original=root.parent.parent/'points-policy-amendment.json'
    if not original.is_file() or original.is_symlink() or original.read_bytes()!=prior_bytes:
        raise ValueError('configured amended predecessor bytes differ')
    v2=predecessor['predecessor_json'].encode()
    for path in (root/'points-policy-predecessor.json',root.parent.parent/'points-policy.json'):
        if not path.is_file() or path.is_symlink() or path.read_bytes()!=v2:
            raise ValueError('original frozen predecessor archive changed')
    archive=root/'points-policy-ema-predecessor.json'
    if archive.exists() and (archive.is_symlink() or not archive.is_file() or archive.read_bytes()!=prior_bytes):
        raise ValueError('EMA predecessor archive differs')
    installed=root/'points-policy.json'
    if not installed.is_file() or installed.is_symlink():
        raise ValueError('EMA transition requires installed amended predecessor')
    if installed.read_bytes()!=encoded(predecessor) and not (installed.read_bytes()==encoded(document) and archive.is_file()):
        raise ValueError('EMA transition requires exact predecessor or journaled successor')
    pending=getattr(adapter,'_publish_intent',None)
    if pending is not None and pending.load() is not None:
        raise ValueError('pending publication forbids EMA transition')
    for name in ('starter-pending.json','starter-intent.json'):
        if (root/name).exists():raise ValueError('pending starter publication forbids EMA transition')
    ledger=getattr(adapter,'_commit_ledger',None) or CommitLedger(root.parent.parent/'ticket-14/state/commit-ledger.json')
    if ledger.load():raise ValueError('sealed ledger requires separate EMA reconciliation')
    anchor=document['activation_anchor'];chain=adapter.runner_chain
    if anchor['block']>chain.head_block() or chain.block_hash(anchor['block'])!=anchor['hash']:
        raise ValueError('EMA anchor is not agreed finalized history')
    # Never revalue a previously retained accepted/terminal receipt under /4.
    # Existing context includes all priced receipt facts, not only current day.
    for path in root.rglob('*.json'):
        if path.is_symlink() or not path.is_file():raise ValueError('unsafe retained EMA transition graph')
        if path.stat().st_size>64*1024**2:raise ValueError('unbounded retained transition member')
        value=json.loads(path.read_bytes(),object_pairs_hook=amendment._unique)
        if not isinstance(value,dict):continue
        for name in ('foreign_valuations','receiptDispositions','pointsReceipts'):
            rows=value.get(name,[])
            if type(rows) is not list:raise ValueError('malformed retained receipt population')
            for row in rows:
                if type(row) is not dict or type(row.get('block')) is not int:raise ValueError('unknown retained receipt identity')
                if row['block']>=document['effective_block']:
                    raise ValueError('retained receipt at or after new boundary forbids repricing')
    confirm_current(adapter,root,allowed_modes=('legacy',amendment.MODE),predecessor_sha256=document['predecessor_sha256'])
    return predecessor


def main():
    import argparse
    from pathlib import Path
    from ..core.calculation_record import encoded
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predecessor',type=Path,required=True)
    parser.add_argument('--predecessor-sha256',required=True)
    parser.add_argument('--anchor-block',type=int,required=True)
    parser.add_argument('--anchor-hash',required=True)
    parser.add_argument('--ema-source',choices=sorted(EMA_SOURCES),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();before=args.predecessor.read_bytes()
    if hashlib.sha256(before).hexdigest()!=args.predecessor_sha256:raise ValueError('predecessor digest differs')
    result=encoded(build(before,{'block':args.anchor_block,'hash':args.anchor_hash},ema_source=args.ema_source))
    if args.predecessor.read_bytes()!=before:raise ValueError('predecessor changed')
    with args.output.open('xb') as stream:stream.write(result)
    print(json.dumps(dict(status='prepared-only',sha256=hashlib.sha256(result).hexdigest(),canonical_writes=False)))


if __name__=='__main__':main()
