"""Fresh paired chain reconciliation for a pre-first-points amendment preview."""
import json
from types import SimpleNamespace

from ..chain.sources import SourceDisagreement, Unverified
from ..core.calculation_record import verify
from ..core.direct import reconcile, same_address
from ..core.reader import ChainReader
from ..starter.publication import verify_overlay


def confirm_legacy_current(adapter, root):
    return confirm_current(adapter, root, allowed_modes=("legacy",))


def confirm_current(adapter, root, *, allowed_modes, predecessor_sha256=None):
    active = json.loads((root / 'starter-active.json').read_bytes())
    reference, marker = active['reference'], active['marker']
    manifest = verify(root, reference)
    proof = verify_overlay(root, reference)
    from .mode import source_mode
    selected, context, _ = source_mode(root, reference)
    if selected not in allowed_modes or (proof is not None and proof['mode'] != 'ordinary-fallback'):
        raise ValueError('amendment requires reconciled ordinary predecessor current source')
    if selected != 'legacy' and context.get('points_policy', {}).get('sha256') != predecessor_sha256:
        raise ValueError('current predecessor policy differs')
    if (manifest['publishable_weights_u16'] != marker['vector'] or manifest['day'] != marker['day']
            or marker.get('result') is None):
        raise ValueError('legacy current source differs from retained receipt')
    chain = adapter.runner_chain
    sources = getattr(chain, '_sources', None)
    if not isinstance(sources, dict) or len(sources) < 2:
        raise Unverified('amendment requires paired publication reconciliation')
    head = chain.head_block()
    head_hash = chain.block_hash(head)
    answers = []
    for source in sources.values():
        connection = source._connection
        class Raw:
            @staticmethod
            def rpc_request(method, params):
                from ..chain.token_context import rpc
                return rpc(connection, method, params)
        class Client:
            # Direct confirmation uses only these read methods, including raw
            # public chain extrinsic hashes. No wallet or send API is exposed.
            _client = SimpleNamespace(_substrate=SimpleNamespace(raw=Raw()))
            @staticmethod
            def _call(value):
                return value
            @staticmethod
            def query(item, params=None, block=None):
                if block is None:
                    raise ValueError('transition reads require exact block')
                at = source.block_hash(block)
                return connection.query(item[0], item[1], params or [], block_hash=at)
            @staticmethod
            def block_info(block):
                at = source.block_hash(block)
                value = connection.get_block(block_hash=at)
                from ..chain.live import _plain
                return SimpleNamespace(number=block, hash=at, extrinsics=[_plain(x) for x in value['extrinsics']])
        reader = ChainReader(Client())
        confirmed = reconcile(reader, marker, finalized_chain=chain)
        from ..chain.live import _plain
        def query(name, params):
            return _plain(connection.query('SubtensorModule', name, params, block_hash=head_hash))
        updates = query('LastUpdate', [marker['netuid']])
        uid = marker['uid']
        if (type(uid) is not int or not isinstance(updates, list) or uid >= len(updates)
                or type(updates[uid]) is not int or updates[uid] != confirmed['block']):
            raise ValueError('chain publication changed or lacks a retained current receipt')
        if not same_address(query('Keys', [marker['netuid'], uid]), marker['hotkey']):
            raise ValueError('current publication identity changed')
        weights = query('Weights', [marker['netuid'] + 4096 * marker['mecid'], uid])
        if sorted(tuple(pair) for pair in weights) != sorted(confirmed['weights'].items()):
            raise ValueError('current applied vector differs from legacy receipt')
        if source.block_hash(head) != head_hash:
            raise ValueError('publication reconciliation head changed')
        answers.append((confirmed, updates[uid], weights))
    if any(answer != answers[0] for answer in answers[1:]):
        raise SourceDisagreement('paired publication reconciliation differs')
    return dict(head=head, block_hash=head_hash, last_update=answers[0][1], source=reference)
