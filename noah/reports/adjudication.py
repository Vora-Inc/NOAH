"""Paired report reconstruction. Results never authorize a weight transaction.

Only the report-only policy is accepted for new provider requests. Deposit and
financial eligibility belong to the ordinary scoring pipeline.
"""
from pathlib import Path
import gzip
import json
import os
import shutil
import copy

from .adjudication_contract import require, strict_json, validate_invocation, validate_request
from .artifacts import canonical_bytes, bytes_sha256, engine_binding
from .common import digest
from .gate import PASS_REASONS, FAILURE_REASONS
from .package_inventory import ReportPackageLimits, validate_package_inventory
from .backing.protocol.preimage import REPORT_POLICY

MANIFEST_SCHEMA = 'noah.report-adjudication-manifest/1'
RESULT_SCHEMA = 'noah.report-adjudication-result/1'
# Separate report bounds; these do not increase ordinary score-evidence limits.
# Final production-window qualification must retain observed sizes below them.
LIMITS = ReportPackageLimits(manifest_bytes=16*1024**2, member_bytes=64*1024**2,
    expanded_member_bytes=128*1024**2, total_bytes=256*1024**2,
    total_expanded_bytes=512*1024**2, members=500000, path_depth=12,
    json_depth=64, line_bytes=8*1024**2)
ANCESTRY_LIMIT = 10000
ROSTER_LIMIT = 4096
MAX_ROUNDS = 128


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def _read(path):
    from .adjudication_contract import strict_manifest_json, MAX_REQUEST_BYTES
    manifest = path.name == 'manifest.json'
    limit = LIMITS.manifest_bytes if manifest else MAX_REQUEST_BYTES
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    return strict_manifest_json(raw) if manifest else strict_json(raw)


def _equal_file(path, value):
    require(path.read_bytes() == canonical_bytes(value), 'reconstructed artifact differs')


def _inventory(root):
    """Inventory only newly created private producer output, never follow links."""
    entries = []
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'package symlink')
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if name == 'manifest.json':
            continue
        require(path.is_file() and path.stat().st_nlink == 1, 'package regular file')
        raw = path.read_bytes()
        require(0 < len(raw) <= LIMITS.member_bytes, 'package member bytes')
        if name.endswith('.gz'):
            with gzip.open(path, 'rb') as handle:
                expanded = handle.read(LIMITS.expanded_member_bytes + 1)
            require(len(expanded) <= LIMITS.expanded_member_bytes, 'package expansion limit')
            kind = 'rpc_responses' if path.name == 'responses.jsonl.gz' else 'receipts'
            encoding = 'gzip'
        else:
            expanded, encoding = raw, 'identity'
            kind = 'manifest' if path.name == 'manifest.json' else ('metadata' if path.name == 'metadata.hex' else 'json')
        entries.append({'path': name, 'kind': kind, 'encoding': encoding,
            'bytes': len(raw), 'sha256': bytes_sha256(raw),
            'expanded_bytes': len(expanded), 'expanded_sha256': bytes_sha256(expanded)})
    return entries


def _policy(request):
    identity = validate_request(request)
    require(request['policy']['eligibility_policy_version'] == REPORT_POLICY,
            'new adjudication requires report-only policy')
    return identity


def _source_session(endpoint, pin, operation, path, label, budget):
    from .backing.rpc import RpcClient, close_sockets
    from .backing.source import LiveSource, ReplaySource, save_receipts
    client = RpcClient([endpoint], '0x' + pin, budget.transport, run_budget_s=min(180, budget.remaining()))
    try:
        live = operation(LiveSource(client))
        for row in client.receipts:
            row.endpoint = label
        save_receipts(str(path), client.receipts)
        rebuilt = operation(ReplaySource([row.to_json() for row in client.receipts], '0x' + pin))
        require(canonical_bytes(live) == canonical_bytes(rebuilt), 'raw session replay differs')
        return rebuilt
    finally:
        close_sockets()


def _roster_operation(contract):
    from .roster import read_snapshot_roster
    return lambda source: read_snapshot_roster(source, contract, max_members=ROSTER_LIMIT)


def _seat_operation(request, seat):
    from .roster import read_scored_seat
    return lambda source: read_scored_seat(source, seat,
        genesis_hash=request['genesis_hash'], mining_netuid=request['mining_netuid'])


def _seat_groups(request):
    groups = {}
    for seat in request['scored_seats']:
        pin = seat['observed_at']['block_hash']
        group = groups.setdefault(pin, [])
        require(not group or group[0]['observed_at'] == seat['observed_at'], 'seat observation hash/height differs')
        group.append(seat)
    return sorted(groups.items())


def _seat_group_operation(request, seats):
    def operation(source):
        class Cached:
            def __init__(self):
                self.rows = {}
            def call(self, method, params):
                key = (method, json.dumps(params, sort_keys=True))
                if key not in self.rows:
                    self.rows[key] = copy.deepcopy(source.call(method, params))
                return copy.deepcopy(self.rows[key])
        frozen = Cached()
        return [_seat_operation(request, seat)(frozen) for seat in seats]
    return operation


def _replay_session(path, pin, operation):
    from .backing.source import ReplaySource, load_receipts
    return operation(ReplaySource(load_receipts(str(path)), '0x' + pin))


def _collect_reader(request, contract, source, root, budget):
    from .backing.collector import collect
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    roster = _source_session(source['endpoint'], contract['block_hash'],
        _roster_operation(contract), root / 'roster-receipts.jsonl.gz', source['id'], budget)
    _write(root / 'roster.json', roster)
    require(roster['members'], 'empty required roster')
    collected = collect(contract, [source['endpoint']], root / 'temporary',
        [member['hotkey'] for member in roster['members']], evaluation=request['evaluation'],
        evaluation_ancestor_limit=ANCESTRY_LIMIT, budget=budget, reader_label=source['id'])
    Path(collected['directory']).rename(root / 'collection')
    shutil.rmtree(root / 'temporary')  # Only this new producer-owned scratch tree.
    _write(root / 'manifest.json', {'schema': 'noah.report-reader-package/1',
        'round_sha256': digest(contract), 'reader': source['id'], 'files': _inventory(root)})


def _reconstruct_reader(request, contract, root):
    from .backing.offline import verify_package
    roster = _replay_session(root / 'roster-receipts.jsonl.gz', contract['block_hash'], _roster_operation(contract))
    _equal_file(root / 'roster.json', roster)
    package = root / 'collection'
    require(_read(package / 'round.json') == contract, 'reader round differs')
    require(_read(package / 'hotkeys.json') == [member['hotkey'] for member in roster['members']],
            'collection does not cover complete snapshot roster')
    report = verify_package(package, include_evidence=True)
    require(report['status'] == 'RECONSTRUCTED' and report.get('engine_retained') is True,
            'reader package does not reconstruct retained engine')
    require(report.get('evaluation_proof', {}).get('evaluation') == request['evaluation'],
            'reader cutoff differs')
    require(set(report['verdicts']) == {row['hotkey'] for row in roster['members']},
            'reader verdict coverage differs')
    _bind_collection_roster(roster, report)
    _equal_file(root / 'manifest.json', {'schema': 'noah.report-reader-package/1',
        'round_sha256': digest(contract), 'reader': root.name, 'files': _inventory(root)})
    return {'roster': roster, 'report': report,
            'manifest_sha256': bytes_sha256((root / 'manifest.json').read_bytes())}


def _bind_collection_roster(roster, report):
    for member in roster['members']:
        snapshot = report['evidence'][member['hotkey']].get('snapshot', {})
        require(snapshot.get('registered') is True and all(snapshot.get(key) == member[key]
                for key in ('hotkey', 'coldkey')), 'collector identity differs from authenticated roster')


def _check_stream_consistency(paths):
    """One reader cannot contradict its own immutable reads across sessions."""
    from .backing.rpc import canonical_result
    index = {}
    for path in paths:
        with gzip.open(path, 'rt') as handle:
            for line in handle:
                row = json.loads(line)
                method, params = row.get('method'), row.get('params') or []
                if method == 'state_getStorageAt':
                    method = 'state_getStorage'  # Same immutable storage read, including cross-SDK sessions.
                if method == 'chain_getBlockHash' and len(params) == 1 and params[0] is not None:
                    value = params[0]
                    params = [int(value, 16) if isinstance(value, str) and value.startswith('0x') else value]
                elif method in ('chain_getHeader', 'chain_getBlock', 'state_getStorage',
                                'state_getStorageHash', 'state_getRuntimeVersion',
                                'state_getMetadata', 'state_getKeysPaged', 'state_call'):
                    if not params or not isinstance(params[-1], str) or len(params[-1]) != 66:
                        continue  # Unpinned latest-head reads legitimately change.
                else:
                    continue
                request_identity = method + '\n' + canonical_result(params)
                value = bytes_sha256(canonical_result(row['result']).encode('utf-8'))
                require(request_identity not in index or index[request_identity] == value, 'reader contradicts immutable cross-session response')
                index[request_identity] = value


def resolve_round(request, contract, readers):
    """Pure resolver over independently reconstructed inputs, not saved verdicts."""
    identity = digest(contract)
    a, b = readers['reader_a'], readers['reader_b']
    require(a['roster'] == b['roster'], 'paired snapshot roster disagreement')
    roster = a['roster']
    engines_agree = canonical_bytes(a['report']['engine']) == canonical_bytes(b['report']['engine'])
    binding = engine_binding(contract, a['report']['engine']) if engines_agree else None
    rows = []
    for member in roster['members']:
        hotkey = member['hotkey']
        verdicts = [readers[label]['report']['verdicts'][hotkey] for label in ('reader_a', 'reader_b')]
        status, reason = 'UNVERIFIABLE', 'independent_readers_disagree'
        if engines_agree and verdicts[0] == verdicts[1]:
            status, reason = verdicts[0]['status'], verdicts[0]['reason']
        key = (status, reason)
        effect = 'zero' if key in FAILURE_REASONS else 'preserve'
        outcome = ('confirmed_failure' if effect == 'zero' else
                   'verified' if key in PASS_REASONS else 'unresolved')
        rows.append({**member, 'effect': effect, 'outcome': outcome, 'status': status, 'reason': reason,
            'sources': {label: {'verdict_sha256': digest(readers[label]['report']['verdicts'][hotkey]),
                               'evidence_sha256': digest(readers[label]['report']['evidence'][hotkey])}
                        for label in ('reader_a', 'reader_b')}})
    required = {'schema': 'noah.report-required-roster/1', 'request_sha256': digest(request),
        'round_sha256': identity, 'evaluation': request['evaluation'], 'snapshot': roster,
        'required_members': roster['members']}
    verdicts = {'schema': 'noah.report-final-verdicts/1', 'request_sha256': digest(request),
        'round_sha256': identity, 'evaluation': request['evaluation'],
        'required_roster_sha256': bytes_sha256(canonical_bytes(required)),
        'engine_sha256': binding['engine_sha256'] if binding else None, 'rows': rows}
    summary = {'round_sha256': identity, 'required_roster_sha256': verdicts['required_roster_sha256'],
        'evaluation': request['evaluation'],
        'readers': {label: readers[label]['manifest_sha256'] for label in ('reader_a', 'reader_b')},
        'engine_sha256': verdicts['engine_sha256'],
        'calculation_sha256': binding['calculation_sha256'] if binding else None,
        'verdicts_sha256': bytes_sha256(canonical_bytes(verdicts)),
        'resolved': all(row['outcome'] != 'unresolved' for row in rows)}
    return summary, {'contract': contract, 'required_roster': required,
        'engine': a['report']['engine'] if engines_agree else None,
        'verdicts': verdicts, 'evaluation': request['evaluation']}


def _assigned_targets(request, seat):
    if 'assignments' not in request:
        return None
    identity = (seat['hotkey'], seat['coldkey'], seat['registration_block'])
    for row in request['assignments']:
        if (row['hotkey'], row['coldkey'], row['registration_block']) == identity:
            return set(row['targets'])
    return set()


def resolve_seats(request, artifacts):
    decisions = []
    for seat in request['scored_seats']:
        assigned = _assigned_targets(request, seat)
        rounds = []
        for artifact in artifacts:
            contract = artifact['contract']
            row = {'round_sha256': digest(contract), 'effect': 'preserve',
                   'outcome': 'not_required', 'status': None, 'reason': 'registered_after_snapshot'}
            if assigned is not None and contract['reported_netuid'] not in assigned:
                row['reason'] = 'not_assigned'
            elif seat['registration_block'] <= contract['block_number']:
                matches = [member for member in artifact['verdicts']['rows']
                           if all(member[field] == seat[field] for field in ('hotkey', 'coldkey', 'registration_block'))]
                require(len(matches) == 1, 'scored incarnation absent or ambiguous in snapshot roster')
                row.update({field: matches[0][field] for field in ('effect', 'outcome', 'status', 'reason')})
            rounds.append(row)
        outcome = ('confirmed_failure' if any(row['effect'] == 'zero' for row in rounds) else
                   'unresolved' if any(row['outcome'] == 'unresolved' for row in rounds) else
                   'verified' if any(row['outcome'] == 'verified' for row in rounds) else 'not_required')
        decisions.append({**{key: seat[key] for key in ('uid', 'hotkey', 'coldkey', 'registration_block')},
            'effect': 'zero' if outcome == 'confirmed_failure' else 'preserve',
            'outcome': outcome, 'rounds': rounds})
    return decisions


def _semantic_reconstruction(request, root, *, budget=None, shared=False):
    artifacts, summaries = [], []
    for label in ('reader_a', 'reader_b'):
        paths = [root / 'seats' / label / f'{pin}.jsonl.gz' for pin, _ in _seat_groups(request)]
        if shared:
            from .shared_window import stream_paths
            paths.extend(stream_paths(request, root, label))
        else:
            for contract in request['policy']['required_rounds']:
                directory = root / 'rounds' / digest(contract) / label
                paths.extend([directory / 'roster-receipts.jsonl.gz',
                              directory / 'collection' / 'receipts.jsonl.gz',
                              directory / 'collection' / 'responses.jsonl.gz'])
        _check_stream_consistency(paths)
    for pin, seats in _seat_groups(request):
        if budget:
            budget.remaining()
        for label in ('reader_a', 'reader_b'):
            path = root / 'seats' / label / f"{pin}.jsonl.gz"
            rebuilt = _replay_session(path, pin, _seat_group_operation(request, seats))
            require(rebuilt == seats, 'scored identity differs')
    if shared:
        from .shared_window import reconstruct_readers
        paired = {label: reconstruct_readers(request, root, label, budget=budget) for label in ('reader_a', 'reader_b')}
    for contract in request['policy']['required_rounds']:
        if budget:
            budget.remaining()
        directory = root / 'rounds' / digest(contract)
        readers = ({label: paired[label][digest(contract)] for label in ('reader_a', 'reader_b')} if shared else
                   {label: _reconstruct_reader(request, contract, directory / label)
                    for label in ('reader_a', 'reader_b')})
        summary, artifact = resolve_round(request, contract, readers)
        summaries.append(summary)
        artifacts.append(artifact)
    decisions = resolve_seats(request, artifacts)
    return summaries, decisions, artifacts


def _manifest(request, root, summaries, decisions, *, schema=MANIFEST_SCHEMA):
    return {'schema': schema, 'request_sha256': digest(request),
        'policy_sha256': request['policy_sha256'], 'request': request,
        'status': 'COMPLETE' if all(row['resolved'] for row in summaries) else 'UNRESOLVED',
        'rounds': summaries, 'scored_decisions': decisions, 'files': _inventory(root)}


def _expected_members(request):
    names = {'manifest.json'}
    collection = ('evidence.json metadata.hex engine.json engine-binding.json evaluation.json '
                  'evaluation-proof.json coverage.json receipts.jsonl.gz hotkeys.json '
                  'responses.jsonl.gz collection.json round.json').split()
    for label in ('reader_a', 'reader_b'):
        names.update(f'seats/{label}/{pin}.jsonl.gz' for pin, _ in _seat_groups(request))
    for contract in request['policy']['required_rounds']:
        prefix = 'rounds/' + digest(contract) + '/'
        names.update(prefix + item for item in ('required-roster.json', 'final-verdicts.json'))
        count = request['evaluation']['block_number'] - contract['block_number'] + 1
        require(0 < count <= 10001, 'package report-window block limit')
        for label in ('reader_a', 'reader_b'):
            reader = prefix + label + '/'
            names.update(reader + item for item in ('manifest.json', 'roster.json', 'roster-receipts.jsonl.gz'))
            names.update(reader + 'collection/' + item for item in collection)
            names.update(reader + f'collection/block-{number}.json'
                for number in range(contract['block_number'], request['evaluation']['block_number'] + 1))
    require(len(names) <= LIMITS.members + 1, 'semantic package member limit')
    return names


def verify_adjudication(package, request, manifest_sha256, *, include_round_artifacts=False):
    """Offline raw replay against externally pinned manifest and exact request."""
    _policy(request)
    from .package_snapshot import snapshot_package
    with snapshot_package(package, manifest_sha256=manifest_sha256, limits=LIMITS) as root:
        return _verify_snapshot(root, request, manifest_sha256, include_round_artifacts=include_round_artifacts)


def _verify_snapshot(root, request, manifest_sha256, *, include_round_artifacts=False):
    manifest = _read(root / 'manifest.json')
    from .shared_window import SCHEMA as SHARED_SCHEMA, expected_members
    schema = manifest.get('schema')
    require(schema in (MANIFEST_SCHEMA, SHARED_SCHEMA), 'unsupported adjudication package schema')
    shared = schema == SHARED_SCHEMA
    validate_package_inventory(root, manifest_sha256=manifest_sha256, limits=LIMITS,
                               expected_members=expected_members(request) if shared else _expected_members(request))
    require(manifest.get('request') == request, 'manifest answers a different request')
    summaries, decisions, artifacts = (_semantic_reconstruction(request, root, shared=True) if shared else
                                       _semantic_reconstruction(request, root))
    for artifact in artifacts:
        directory = root / 'rounds' / digest(artifact['contract'])
        _equal_file(directory / 'required-roster.json', artifact['required_roster'])
        _equal_file(directory / 'final-verdicts.json', artifact['verdicts'])
    expected = _manifest(request, root, summaries, decisions, schema=schema)
    require(canonical_bytes(manifest) == canonical_bytes(expected), 'manifest differs from raw reconstruction')
    # Recheck immutable bytes after replay, including sources used by decoders.
    validate_package_inventory(root, manifest_sha256=manifest_sha256, limits=LIMITS)
    result = {key: expected[key] for key in ('request_sha256', 'policy_sha256', 'status', 'rounds', 'scored_decisions')}
    return {'schema': RESULT_SCHEMA, 'manifest_sha256': manifest_sha256, **result,
            **({'round_artifacts': artifacts} if include_round_artifacts else {})}


def adjudicate(invocation, output):
    from .budget import ProviderBudget
    budget = ProviderBudget()
    try:
        return _adjudicate(invocation, output, budget)
    finally:
        budget.close()


def _adjudicate(invocation, output, budget):
    validate_invocation(invocation)
    request = invocation['request']
    _policy(request)
    require(len(request['policy']['required_rounds']) <= MAX_ROUNDS and len(request['scored_seats']) <= ROSTER_LIMIT,
            'invocation round/seat resource limit')
    require(all(source['history'] is None for source in invocation['sources']), 'report provider does not accept deposit history')
    from .shared_window import eligible, collect_readers, SCHEMA as SHARED_SCHEMA
    shared = eligible(request)
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for source in invocation['sources']:
        directory = root / 'seats' / source['id']
        directory.mkdir(parents=True, exist_ok=False)
        for pin, seats in _seat_groups(request):
            _source_session(source['endpoint'], pin, _seat_group_operation(request, seats),
                directory / f"{pin}.jsonl.gz", source['id'], budget)
        if shared:
            collect_readers(request, source, root, budget)
        else:
            for contract in request['policy']['required_rounds']:
                _collect_reader(request, contract, source, root / 'rounds' / digest(contract) / source['id'], budget)
    summaries, decisions, artifacts = _semantic_reconstruction(request, root, budget=budget, shared=shared)
    for artifact in artifacts:
        directory = root / 'rounds' / digest(artifact['contract'])
        _write(directory / 'required-roster.json', artifact['required_roster'])
        _write(directory / 'final-verdicts.json', artifact['verdicts'])
    _write(root / 'manifest.json', _manifest(request, root, summaries, decisions,
           schema=SHARED_SCHEMA if shared else MANIFEST_SCHEMA))
    result = verify_adjudication(root, request, bytes_sha256((root / 'manifest.json').read_bytes()))
    budget.remaining()
    # Persist files and directory entries before returning a complete package.
    for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True) + [root]:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if path.is_dir() else 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return result
