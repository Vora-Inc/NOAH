"""Weekly package/2: one raw B-to-evaluation window per independent reader.

This is not a verdict cache. Each target reuses only the window reconstructed
from that reader's raw RPC bytes; its engine, context selection and verdicts
are recomputed. Package/1 remains the historical producer/replay path.
"""
from pathlib import Path
import shutil

from .adjudication_contract import require
from .artifacts import bytes_sha256, canonical_bytes
from .common import digest

SCHEMA = 'noah.report-adjudication-manifest/2'
READER_SCHEMA = 'noah.report-reader-package/2'
WINDOW_SCHEMA = 'noah.report-shared-window/1'
COLLECTION = ('engine.json engine-binding.json evaluation.json '
              'evaluation-proof.json receipts.jsonl.gz hotkeys.json '
              'responses.jsonl.gz collection.json round.json').split()
SHARED = ('coverage.json roster.json roster-receipts.jsonl.gz responses.jsonl.gz metadata.hex manifest.json').split()


def window_binding(contract, evaluation, limit):
    from .verification import validate_round
    validate_round(contract)
    require(contract.get('commitment_format') == 'context-v1', 'shared window requires context commitments')
    require(type(evaluation) is dict and limit == 10000, 'shared window requires bounded frozen evaluation')
    # The two omitted fields identify the target, not its common chain window.
    common = {k: v for k, v in contract.items() if k not in ('reported_netuid', 'release_prefix')}
    return {'schema': 'noah.report-shared-window-binding/1', 'round': common,
            'evaluation': evaluation, 'max_ancestor_distance': limit}


def eligible(request):
    rounds = request['policy']['required_rounds']
    if 'assignments' not in request or not 2 <= len(rounds) <= 128:
        return False
    if any(row.get('commitment_format') != 'context-v1' for row in rounds):
        return False
    bindings = [window_binding(row, request['evaluation'], 10000) for row in rounds]
    return all(row == bindings[0] for row in bindings)


def expected_members(request):
    from .adjudication import _seat_groups, LIMITS
    require(eligible(request), 'shared package has incompatible rounds')
    names = {'manifest.json'}
    for label in ('reader_a', 'reader_b'):
        names.update('shared/' + label + '/' + item for item in SHARED)
        names.update(f'seats/{label}/{pin}.jsonl.gz' for pin, _ in _seat_groups(request))
    for contract in request['policy']['required_rounds']:
        prefix = 'rounds/' + digest(contract) + '/'
        names.update(prefix + item for item in ('required-roster.json', 'final-verdicts.json'))
        for label in ('reader_a', 'reader_b'):
            names.add(prefix + label + '/manifest.json')
            names.update(prefix + label + '/collection/' + item for item in COLLECTION)
    require(len(names) <= LIMITS.members + 1, 'semantic shared member limit')
    count = request['evaluation']['block_number'] - request['policy']['required_rounds'][0]['block_number'] + 1
    require(0 < count <= 10001, 'shared report-window block limit')
    return names


def _shared_manifest(root, request, label):
    from .adjudication import _inventory
    first = request['policy']['required_rounds'][0]
    return {'schema': WINDOW_SCHEMA, 'reader': label,
            'binding': window_binding(first, request['evaluation'], 10000), 'files': _inventory(root)}


def _reader_manifest(root, contract, label, shared_sha):
    from .adjudication import _inventory
    return {'schema': READER_SCHEMA, 'round_sha256': digest(contract), 'reader': label,
            'shared_manifest_sha256': shared_sha, 'files': _inventory(root)}


def collect_readers(request, source, root, budget):
    from .adjudication import _write, _source_session, _roster_operation
    from .backing.collector import _connect, _collect, _response_file_bytes
    from .backing.rpc import close_sockets
    label = source['id']
    common = root / 'shared' / label
    common.mkdir(parents=True, exist_ok=False, mode=0o700)
    first = request['policy']['required_rounds'][0]
    roster = _source_session(source['endpoint'], first['block_hash'], _roster_operation(first),
                             common / 'roster-receipts.jsonl.gz', label, budget)
    require(roster['members'], 'empty required roster')
    _write(common / 'roster.json', roster)
    substrate = None
    window = None
    try:
        substrate, _ = _connect([source['endpoint']], budget=budget)
        for contract in request['policy']['required_rounds']:
            budget.remaining()
            reader = root / 'rounds' / digest(contract) / label
            reader.mkdir(parents=True, exist_ok=False, mode=0o700)
            collected = _collect(substrate, label, [source['endpoint']], contract, reader / 'temporary',
                [member['hotkey'] for member in roster['members']], lambda: 0,
                evaluation=request['evaluation'], evaluation_ancestor_limit=10000,
                budget=budget, capture_window=True, shared_window=window)
            if window is None:
                window = collected['shared_window']
                # Only bytes recorded before target calculation belong to the shared window.
                _exclusive(common / 'responses.jsonl.gz',
                           _response_file_bytes(substrate._response_log[:window['response_count']]))
                _exclusive(common / 'metadata.hex', window['raw_metadata'].encode())
                _exclusive(common / 'coverage.json', _coverage(window))
                _write(common / 'manifest.json', _shared_manifest(common, request, label))
                shared_sha = bytes_sha256((common / 'manifest.json').read_bytes())
            Path(collected['directory']).rename(reader / 'collection')
            shutil.rmtree(reader / 'temporary')  # New private producer-owned scratch only.
            _write(reader / 'manifest.json', _reader_manifest(reader, contract, label, shared_sha))
            substrate._response_log.clear()
    finally:
        for close in ([substrate.close] if substrate is not None else []) + [close_sockets]:
            try:
                close()
            except Exception:
                pass


def _exclusive(path, raw):
    from .backing.collector import _exclusive as write
    write(path, raw)


def reconstruct_readers(request, root, label, *, budget=None):
    """Replay one reader's shared raw window, then every target's separate inputs."""
    from .adjudication import (_read, _equal_file, _replay_session, _roster_operation,
                              _bind_collection_roster)
    from .backing.offline import (_load_responses, _check_binding, _index, _ReplaySocket,
                                 _check_integrity, _evaluation_inputs, report_from_result)
    from .backing.collector import _collect
    from .backing.source import ReplaySource, load_receipts
    from substrateinterface import SubstrateInterface
    common = root / 'shared' / label
    first = request['policy']['required_rounds'][0]
    roster = _replay_session(common / 'roster-receipts.jsonl.gz', first['block_hash'], _roster_operation(first))
    _equal_file(common / 'roster.json', roster)
    _equal_file(common / 'manifest.json', _shared_manifest(common, request, label))
    shared_sha = bytes_sha256((common / 'manifest.json').read_bytes())
    rows = _load_responses(common / 'responses.jsonl.gz')
    _check_binding(first, rows)
    for contract in request['policy']['required_rounds']:
        directory = root / 'rounds' / digest(contract) / label / 'collection'
        rows.extend(_incremental_responses(directory / 'responses.jsonl.gz'))
    substrate = SubstrateInterface(websocket=_ReplaySocket(_index(rows)), auto_discover=False)
    substrate.init_runtime()
    window = None
    outputs = {}
    try:
        for contract in request['policy']['required_rounds']:
            if budget:
                budget.remaining()
            reader = root / 'rounds' / digest(contract) / label
            directory = reader / 'collection'
            require(_read(directory / 'round.json') == contract, 'shared reader round differs')
            hotkeys = [member['hotkey'] for member in roster['members']]
            require(_read(directory / 'hotkeys.json') == hotkeys, 'shared roster coverage differs')
            receipts = load_receipts(str(directory / 'receipts.jsonl.gz'))
            for row in receipts:
                _check_integrity(row)
            settings = _evaluation_inputs(directory)
            require(settings == {'evaluation': request['evaluation'], 'max_ancestor_distance': 10000},
                    'shared evaluation settings differ')
            result = _collect(substrate, 'retained-package', [], contract, None, hotkeys, lambda: 0,
                chain_source=ReplaySource(receipts, '0x' + contract['block_hash']),
                evaluation=request['evaluation'], evaluation_ancestor_limit=10000,
                capture_window=True, shared_window=window)
            if window is None:
                window = result['shared_window']
                require((common / 'metadata.hex').read_bytes() == window['raw_metadata'].encode(),
                        'shared metadata differs from raw replay')
                require((common / 'coverage.json').read_bytes() == _coverage(window),
                        'shared coverage differs from raw replay')
            report = report_from_result(directory, contract, result, settings, include_evidence=True)
            require(report['status'] == 'RECONSTRUCTED' and report.get('engine_retained') is True,
                    'shared reader does not reconstruct retained engine')
            require(report.get('evaluation_proof', {}).get('evaluation') == request['evaluation'],
                    'shared reader cutoff differs')
            require(set(report['verdicts']) == set(hotkeys), 'shared verdict coverage differs')
            _bind_collection_roster(roster, report)
            _equal_file(reader / 'manifest.json', _reader_manifest(reader, contract, label, shared_sha))
            outputs[digest(contract)] = {'roster': roster, 'report': report,
                                        'manifest_sha256': bytes_sha256((reader / 'manifest.json').read_bytes())}
    finally:
        substrate.close()
    return outputs



def _coverage(window):
    return canonical_bytes([{key: row[key] for key in ('hash', 'number', 'timestamp_ms')}
                            for row in window['blocks']])


def _incremental_responses(path):
    # An SDK may need no additional queries after the common window/snapshot.
    # Only this supplemental transcript may be empty; all common/raw engine
    # inputs remain mandatory and are reconstructed independently.
    import gzip
    import json
    from .backing.offline import _check_integrity
    with gzip.open(path, 'rt') as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    for row in rows:
        _check_integrity(row)
    return rows

def stream_paths(request, root, label):
    common = root / 'shared' / label
    paths = [common / 'roster-receipts.jsonl.gz', common / 'responses.jsonl.gz']
    for contract in request['policy']['required_rounds']:
        directory = root / 'rounds' / digest(contract) / label / 'collection'
        paths.extend([directory / 'receipts.jsonl.gz', directory / 'responses.jsonl.gz'])
    return paths
