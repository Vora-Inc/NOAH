"""Prospective same-generation reference proof, distinct from price-cache integrity.

Every positive-duration interval is authenticated through both readers. A
known unsupported interval is terminal for that fixed receipt reference;
unavailable evidence and receipt-block generation ambiguity are pending.
Normal chain-state semantics are trusted, not an assertion about genesis origin.
"""
import hashlib
from dataclasses import asdict
from pathlib import Path

from ..chain.sources import Refusal, SourceLied, Unverified
from ..chain.token_context import CODES, FIELDS, decoded
from ..core.calculation_record import durable_write, encoded
from .pricing import PriceInterval
from .ledger import R

SCHEMA = 'noah-token-reference-proof/1'
MAX_SIZE = 4 * 1024**2


def require(value, message='reference proof malformed'):
    if not value:
        raise ValueError(message)


def compact(context, asset):
    fields = context['assets'][str(asset)]
    # A null raw birth/counter is the authenticated ValueQuery default, not an
    # inferred absence of the subnet. Membership itself must be stored true.
    return [fields[name]['raw'] for name, _ in FIELDS]


def state(values):
    require(isinstance(values, list) and len(values) == 4)
    result = []
    for raw, (_, kind) in zip(values, FIELDS, strict=True):
        result.append((False if kind == 'bool' else 0) if raw is None else decoded(raw, kind))
    return result


def generation(values):
    member, birth, counter, mechanism = state(values)
    if counter == 2**64 - 1:
        raise Unverified('saturated token generation counter cannot establish incarnation')
    if mechanism not in (0, 1):
        raise Unverified('unreviewed token price mechanism')
    return member, (birth, counter)


def unsupported(values, expected):
    member, marker = generation(values)
    if not member:
        return 'reference-subnet-absent'
    if marker != expected:
        return 'reference-token-incarnation-differs'
    return None


def proof_digest(document):
    return hashlib.sha256(encoded(document)).hexdigest()


def validate_reference_proof(document, *, read_proof=None):
    """Validate window/receipt structure; resolve shared dependencies when supplied."""
    from .migration import hash32
    require(isinstance(document, dict))
    common = {'schema', 'genesis', 'native_netuid', 'asset_netuid', 'window', 'status', 'reason'}
    is_receipt = document.get('schema') == 'noah-token-reference-receipt/1'
    extra = ({'receipt', 'receipt_native', 'receipt_asset', 'parent_native', 'parent_asset',
              'runtime', 'parent_runtime', 'metadata_sha256', 'window_proof'} if is_receipt else
             {'expected_native', 'expected_asset', 'metadata', 'rows'})
    require(set(document) == common | extra)
    require(document['schema'] == ('noah-token-reference-receipt/1' if is_receipt else SCHEMA))
    require(hash32(document['genesis']))
    for key in ('native_netuid', 'asset_netuid'):
        require(type(document[key]) is int and 0 <= document[key] < 65536)
    require(document['native_netuid'] > 0 and document['native_netuid'] != document['asset_netuid'])
    window = document['window']
    require(isinstance(window, list) and len(window) == 6)
    start, end, first, first_hash, last, last_hash = window
    require(all(type(x) is int for x in (start, end, first, last)))
    require(start >= 0 and end == start + 86400 and start % 86400 == 0 and 0 < first <= last)
    require(hash32(first_hash) and hash32(last_hash))
    import re
    def sha(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
    require(document['status'] in ('supported', 'unsupported'))
    if is_receipt:
        receipt = document['receipt']
        require(isinstance(receipt, dict) and set(receipt) == {'block', 'block_hash', 'parent_hash', 'timestamp'})
        require(type(receipt['block']) is int and receipt['block'] >= last)
        require(type(receipt['timestamp']) is int and receipt['timestamp'] // 86400 * 86400 == end)
        require(hash32(receipt['block_hash']) and hash32(receipt['parent_hash']))
        for key in ('runtime', 'parent_runtime'):
            value = document[key]
            require(isinstance(value, dict) and set(value) == {'spec_version', 'code_hash', 'transaction_version'})
            require(type(value['spec_version']) is int and value['spec_version'] in CODES
                    and value['code_hash'] == CODES[value['spec_version']]
                    and type(value['transaction_version']) is int and value['transaction_version'] == 1)
        require(sha(document['metadata_sha256']))
        markers = {}
        for role in ('native', 'asset'):
            if role == 'asset' and document['asset_netuid'] == 0:
                require(document['receipt_asset'] is None and document['parent_asset'] is None)
                markers[role] = None
                continue
            present, marker = generation(document['receipt_' + role])
            before, previous = generation(document['parent_' + role])
            require(present and before and marker == previous and marker[0] < receipt['block'])
            markers[role] = list(marker)
        reference = document['window_proof']
        require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'} and sha(reference['sha256']))
        require(reference['path'] == 'points-reference-proofs/' + reference['sha256'] + '.json')
        if read_proof is not None:
            value = read_proof(reference)
            require(type(value) is bytes and len(value) < MAX_SIZE and hashlib.sha256(value).hexdigest() == reference['sha256'])
            import json
            full = json.loads(value)
            require(full.get('schema') == SCHEMA)  # no recursive receipt links or cycles
            require(validate_reference_proof(full) == reference['sha256'])
            require(all(full[k] == document[k] for k in common - {'schema'}))
            require(full['expected_native'] == markers['native'] and full['expected_asset'] == markers['asset'])
    else:
        expected = {}
        for role in ('native', 'asset'):
            marker = document['expected_' + role]
            if role == 'asset' and document['asset_netuid'] == 0:
                require(marker is None)
            else:
                require(isinstance(marker, list) and len(marker) == 2 and all(type(v) is int and 0 <= v < 2**64 - 1 for v in marker))
            expected[role] = None if marker is None else tuple(marker)
        metadata = document['metadata']
        require(isinstance(metadata, dict) and metadata)
        require(all(str(int(spec)) == spec and int(spec) in CODES and sha(value) for spec, value in metadata.items()))
        rows = document['rows']
        require(isinstance(rows, list) and 0 < len(rows) <= last - first + 1)
        cursor, previous, failure = start, None, None
        for row in rows:
            require(isinstance(row, list) and len(row) == 10)
            block, block_hash, left, right, spec, native, asset, pn, pa, reason = row
            require(all(type(x) is int for x in (block, left, right, spec)))
            require(first <= block <= last and (previous is None or block > previous))
            require(hash32(block_hash) and str(spec) in metadata and start <= left < right <= end)
            if block == first: require(block_hash == first_hash)
            if block == last: require(block_hash == last_hash)
            require(state(native)[1] <= block)
            if asset is not None: require(state(asset)[1] <= block)
            actual = unsupported(native, expected['native'])
            if document['asset_netuid'] == 0:
                require(asset is None)
            elif actual is None:
                actual = unsupported(asset, expected['asset'])
            if actual is None:
                require(type(pn) is int and type(pa) is int and 0 <= pn < 2**64 and 0 <= pa < 2**64)
                if document['asset_netuid'] == 0: require(pa == R)
                actual = 'native-reference-price-zero' if pn == 0 else ('foreign-reference-price-zero' if pa == 0 else None)
            else:
                require(pn is None and pa is None)
            require(reason == actual)
            if actual is not None:
                require(failure is None and row is rows[-1])
                failure = actual
            else:
                require(left == cursor)
                cursor = right
            previous = block
        if document['status'] == 'unsupported':
            require(failure is not None and document['reason'] == failure)
        else:
            require(failure is None and document['reason'] == 'complete-same-incarnation-reference' and cursor == end)
    require(len(encoded(document)) < MAX_SIZE)
    return proof_digest(document)


class ReferenceEligibility:
    def __init__(self, chain, native):
        self.chain, self.native = chain, native
        self.completed = {}
        self.windows = {}

    def reset(self):
        self.completed.clear()
        self.windows.clear()

    def prove(self, collector, block, assets, window, *, receipt_blocks=None):
        try:
            return self._prove(collector, block, assets, window, receipt_blocks=receipt_blocks)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SourceLied('reference proof/cache structure contradicts its declared evidence') from exc

    def _prove(self, collector, block, assets, window, *, receipt_blocks=None):
        """At most eight foreign assets, sharing one native/context read per block."""
        assets = sorted(set(assets))
        require(0 < len(assets) <= 8 and self.native not in assets)
        receipt_blocks = {a: block for a in assets} if receipt_blocks is None else receipt_blocks
        require(set(receipt_blocks) == set(assets))
        key = (tuple(sorted(receipt_blocks.items())), tuple(window), tuple(assets))
        # Per-call preparation can be reused by the subsequent scalar traversal.
        # clear() is called at each fixture's workload start, never persisted.
        if key in self.completed:
            return self.completed[key]
        chain = self.chain
        contexts, markers = {}, {}
        for receipt_block in sorted(set(receipt_blocks.values())):
            selected_assets = [a for a in assets if receipt_blocks[a] == receipt_block]
            ids = sorted({self.native, *selected_assets} - {0})
            if receipt_block > chain.head_block() or receipt_block <= 1:
                raise Refusal('receipt lifecycle is not finalized')
            current = chain.token_reference_context(receipt_block, ids)
            parent = chain.token_reference_context(receipt_block - 1, ids)
            if current['parent_hash'] != parent['block_hash'] or current['block_hash'] != chain.block_hash(receipt_block):
                raise SourceLied('receipt lifecycle parent/hash differs')
            marks = {}
            for token in ids:
                now, marker = generation(compact(current, token))
                before, previous = generation(compact(parent, token))
                if not now or not before or marker != previous or marker[0] >= receipt_block:
                    raise Unverified('receipt-block token incarnation is ambiguous; preserve pending')
                marks[token] = marker
            for asset in selected_assets:
                contexts[asset] = (current, parent)
                markers[asset] = (marks[self.native], None if asset == 0 else marks[asset])
        def shared_key(asset):
            return (tuple(window), *markers[asset], asset)
        docs, references = {}, {}
        for asset in assets:
            cache_key = shared_key(asset)
            if cache_key in self.windows:
                references[asset] = self.windows[cache_key]
                continue
            docs[asset] = dict(schema=SCHEMA, genesis=collector.genesis, native_netuid=self.native,
                asset_netuid=asset, window=list(window), expected_native=list(markers[asset][0]),
                expected_asset=None if asset == 0 else list(markers[asset][1]), metadata={},
                rows=[], status='supported', reason='complete-same-incarnation-reference')
        start, end, first, _, last, _ = window
        cursors = dict.fromkeys(assets, start)
        active = set(docs)
        # A warm cache supplies prices only after the entire lifecycle predicate
        # below. It never supplies membership, generation, or zero evidence.
        cached = {}
        for asset in docs:
            path, identity = collector._cache_entry('day', [self.native, asset, *window])
            if path.exists():
                values = collector._read_cached(path, identity)
                cached[asset] = {(r['start'], r['end']): r for r in values}
                require(len(cached[asset]) == len(values))
        for height in (range(first, last + 1) if active else ()):
            left = max(start, collector.timestamp(height))
            right = min(end, collector.timestamp(height + 1)) if height < block else end
            if right <= left:
                continue
            selected = sorted({self.native, *active} - {0})
            context = chain.token_reference_context(height, selected)
            h = chain.block_hash(height)
            if h != context['block_hash']:
                raise SourceLied('reference lifecycle hash differs')
            native_state = compact(context, self.native)
            if state(native_state)[1] > height:
                raise SourceLied("native birth is later than sampled state")
            observed_native = None
            for asset in sorted(active):
                document = docs[asset]
                spec = str(context['runtime']['spec_version'])
                if spec in document['metadata'] and document['metadata'][spec] != context['metadata_sha256']:
                    raise SourceLied('reference metadata changed under identical runtime')
                document['metadata'][spec] = context['metadata_sha256']
                asset_state = None if asset == 0 else compact(context, asset)
                if asset_state is not None and state(asset_state)[1] > height:
                    raise SourceLied('token birth is later than sampled state')
                reason = unsupported(native_state, markers[asset][0])
                if reason is None and asset != 0:
                    reason = unsupported(asset_state, markers[asset][1])
                pn = pa = None
                if reason is None:
                    if left != cursors[asset]:
                        raise Refusal('reference timeline is not contiguous')
                    if asset in cached:
                        row = cached[asset].get((left, right))
                        if row is None:
                            raise SourceLied('cached price timeline differs from authenticated reference')
                        pn, pa = row['noah_price_rao'], row['asset_price_rao']
                        if type(pn) is not int or type(pa) is not int or pn <= 0 or pa <= 0:
                            raise SourceLied('cached prices cannot establish zero reference evidence')
                    else:
                        pn = collector.price(height, self.native)
                        pa = R if asset == 0 else collector.price(height, asset)
                    if observed_native is not None and pn != observed_native:
                        raise SourceLied('shared native price timeline disagrees between asset caches')
                    observed_native = pn
                    reason = 'native-reference-price-zero' if pn == 0 else ('foreign-reference-price-zero' if pa == 0 else None)
                document['rows'].append([height, h, left, right, context['runtime']['spec_version'], native_state, asset_state, pn, pa, reason])
                if reason is not None:
                    document['status'], document['reason'] = 'unsupported', reason
                    active.remove(asset)
                else:
                    cursors[asset] = right
            if not active or all(cursors[a] == end for a in active):
                break
        results = {}
        for asset, document in docs.items():
            fingerprint = validate_reference_proof(document)
            directory = collector.directory.parent / 'points-reference-proofs'
            path = directory / (fingerprint + '.json')
            durable_write(path, encoded(document))
            reference = {'path': 'points-reference-proofs/' + path.name, 'sha256': fingerprint}
            cache_key = shared_key(asset)
            references[asset] = (reference, document['status'], document['reason'])
            self.windows[cache_key] = references[asset]
            if document['status'] == 'supported':
                rows = [asdict(PriceInterval(r[2], r[3], r[7], r[8])) for r in document['rows']]
                cache_path, identity = collector._cache_entry('day', [self.native, asset, *window])
                if cache_path.exists():
                    if collector._read_cached(cache_path, identity) != rows:
                        raise SourceLied('cached reference prices differ from eligibility proof')
                else:
                    durable_write(cache_path, encoded(dict(identity=identity, value=rows, sha256=hashlib.sha256(encoded(rows)).hexdigest())))
        for asset in assets:
            reference, status, reason = references[asset]
            current, parent = contexts[asset]
            actual_block = receipt_blocks[asset]
            receipt_document = dict(schema='noah-token-reference-receipt/1', genesis=collector.genesis,
                native_netuid=self.native, asset_netuid=asset, window=list(window),
                receipt=dict(block=actual_block, block_hash=current['block_hash'], parent_hash=parent['block_hash'], timestamp=collector.timestamp(actual_block)),
                receipt_native=compact(current, self.native), parent_native=compact(parent, self.native),
                receipt_asset=None if asset == 0 else compact(current, asset),
                parent_asset=None if asset == 0 else compact(parent, asset),
                runtime=current['runtime'], parent_runtime=current['parent_runtime'], metadata_sha256=current['metadata_sha256'],
                window_proof=reference, status=status, reason=reason)
            fingerprint = validate_reference_proof(receipt_document)
            path = collector.directory.parent / 'points-reference-proofs' / (fingerprint + '.json')
            durable_write(path, encoded(receipt_document))
            results[asset] = dict(status=status, reason=reason, proof={'path': 'points-reference-proofs/' + path.name, 'sha256': fingerprint})
            self.completed[(actual_block, tuple(window), asset)] = results[asset]
        self.completed[key] = results
        return results

    def result(self, collector, block, asset):
        window = tuple(collector._window(block))
        key = (block, window, asset)
        if key not in self.completed:
            self.prove(collector, block, [asset], window)
        return self.completed[key]


def verify_dispositions(directory, rows, policy, *, read_proof=None):
    """Bind settled output to its immutable complete or terminal reference proof."""
    import json
    from .receipts import validate
    from .pricing import daily_average_credit
    root = Path(directory) if directory is not None else None
    if read_proof is None:
        def read_proof(reference):
            path = root / reference['path']
            if path.is_symlink() or not path.is_file() or path.stat().st_size >= MAX_SIZE:
                raise ValueError('missing/unsafe reference proof')
            return path.read_bytes()
    # Use exactly the authenticated bytes for subsequent arithmetic, even if
    # a filesystem or caller-supplied loader changes during this verification.
    original_reader = read_proof
    verified_bytes = {}
    def read_proof(reference):
        key = (reference['path'], reference['sha256'])
        if key not in verified_bytes:
            data = original_reader(reference)
            require(type(data) is bytes and len(data) < MAX_SIZE)
            require(hashlib.sha256(data).hexdigest() == reference['sha256'])
            verified_bytes[key] = data
        return verified_bytes[key]
    for row in validate(rows):
        reference = row['reference_proof']
        data = read_proof(reference)
        require(type(data) is bytes and len(data) < MAX_SIZE)
        if hashlib.sha256(data).hexdigest() != reference['sha256']:
            raise ValueError('reference proof content differs')
        document = json.loads(data)
        if document.get("schema") != "noah-token-reference-receipt/1" or validate_reference_proof(document, read_proof=read_proof) != reference['sha256']:
            raise ValueError('noncanonical reference proof')
        if (document['genesis'] != policy.migration['genesis']
                or document['native_netuid'] != policy.manifest['netuid']
                or document['asset_netuid'] != row['token_netuid']
                or document['receipt']['block'] != row['block']
                or document['receipt']['block_hash'] != row['block_hash']
                or row['block'] <= policy.migration['cutoff_block']
                or row['day'] <= policy.migration['cutoff_day']
                or row['day'] != (document['receipt']['timestamp'] - int(policy.day_anchor_ts)) // 86400
                or row['receipt_id'] != f"{document['genesis']}/{row['block_hash']}/{row['event_ordinal']}"):
            raise ValueError('receipt and proof identity differ')
        if document['status'] == 'unsupported':
            if row['status'] != 'unsupported' or row['reason'] != document['reason'] or row['credit_rao'] != '0':
                raise ValueError('terminal disposition differs from proof')
        else:
            full = json.loads(read_proof(document['window_proof']))
            intervals = [PriceInterval(r[2], r[3], r[7], r[8]) for r in full['rows']]
            value = daily_average_credit(int(row['amount_rao']), row['token_netuid'], document['receipt']['timestamp'], intervals, native_netuid=document['native_netuid'])
            if row['status'] != 'accepted' or int(row['credit_rao']) != value:
                raise ValueError('accepted credit differs from proven reference')
    return True
