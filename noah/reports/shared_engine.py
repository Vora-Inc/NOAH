"""One reader's immutable raw engine inputs, never shared verdicts or readers."""
import copy

from .common import ReportError
from .backing.rpc import RpcClient, canonical_result
from .backing.rpc_cache import immutable_key
from .backing.source import ReplaySource


def engine_key(method, params, pin):
    key = immutable_key(method, params)
    if key is None:
        return None
    # Header reads may authenticate a newer finalized head; every state read
    # must still name this exact historical calculation snapshot.
    bound = params[0] if method in ('chain_getHeader', 'chain_getBlock', 'state_getRuntimeVersion', 'state_getMetadata') else params[-1]
    return key if method == 'chain_getHeader' or bound == pin else None


class SharedEngineSource:
    def __init__(self, endpoint, label, block_hash, budget):
        self.endpoint, self.label, self.block_hash, self.budget = endpoint, label, block_hash, budget
        self.shared_receipts, self.index, self.shared_bytes = [], {}, 0
        self.receipts = []
        self.client = None
        self._alpha = None

    def alpha_inventory(self, netuid):
        from .backing.reads import alpha_inventory
        for transport in self.budget.connections:
            transport.check_failure()
        self.budget.remaining()
        if self._alpha is None:
            self._alpha = alpha_inventory(self, self.block_hash)
        rows, positions, pages = self._alpha
        return list(rows.get(netuid, [])), pages, positions.get(netuid, 0)

    def begin_round(self):
        self.receipts = []
        self.client = RpcClient([self.endpoint], self.block_hash, self.budget.transport, run_budget_s=180)

    def call(self, method, params):
        for transport in self.budget.connections:
            transport.check_failure()
        self.budget.remaining()
        key = engine_key(method, params, self.block_hash)
        if key is not None and key in self.index:
            return copy.deepcopy(self.index[key].result)
        try:
            result = self.client.call(method, params)
        finally:
            # RpcClient/engine may translate transport exceptions into unavailable
            # outputs; a resource/transport failure must still refuse the package.
            for transport in self.budget.connections:
                transport.check_failure()
        receipt = self.client.receipts[-1]
        receipt.endpoint = self.label
        size = len(canonical_result(receipt.to_json()).encode())
        self.budget.retain(size)
        if key is None:
            self.receipts.append(receipt)
        else:
            if self.shared_bytes + size > self.budget.retained_limit or len(self.index) >= self.budget.max_calls:
                raise ReportError('shared engine receipt bound exceeded')
            self.shared_bytes += size
            self.index[key] = receipt
            self.shared_receipts.append(receipt)
        return copy.deepcopy(result)


class _CopyReplaySource(ReplaySource):
    def call(self, method, params):
        return copy.deepcopy(super().call(method, params))


class SharedReplaySource(_CopyReplaySource):
    """Validate the common inventory once, preserving each target's raw reads."""
    def __init__(self, rows, block_hash):
        for row in rows:
            if engine_key(row.get('method'), row.get('params'), block_hash) is None:
                from .backing.source import ReplayError
                raise ReplayError('shared_engine_requires_immutable_snapshot')
        super().__init__(rows, block_hash)
        self._alpha = None

    def alpha_inventory(self, netuid):
        from .backing.reads import alpha_inventory
        if self._alpha is None:
            self._alpha = alpha_inventory(self, self.block_hash)
        rows, positions, pages = self._alpha
        return list(rows.get(netuid, [])), pages, positions.get(netuid, 0)

    def for_round(self, rows):
        from collections import ChainMap
        from .backing.source import ReplayError
        if any(engine_key(row.get('method'), row.get('params'), self.block_hash) is not None for row in rows):
            raise ReplayError('shared_engine_receipt_in_round')
        local = _CopyReplaySource(rows, self.block_hash)
        for key, row in local.index.items():
            if key in self.index and self.index[key]['sha256'] != row['sha256']:
                from .backing.source import ReplayError
                raise ReplayError('conflicting_response')
        local.index = ChainMap(local.index, self.index)
        local.alpha_inventory = self.alpha_inventory
        return local
