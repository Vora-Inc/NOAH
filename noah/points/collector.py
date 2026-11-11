"""Finalized, dual-source price collection. No signing or trade execution.

Every observed block interval in the prior UTC day is priced. The durable cache
holds completed daily series keyed by chain and block hashes;
neither sampling nor a closing price
is silently substituted for a day average.
"""

import json
from dataclasses import asdict
from pathlib import Path

from ..chain.sources import Refusal, SourceLied
from ..core.calculation_record import digest, durable_write, encoded
from .ledger import R
from .pricing import PriceInterval


class PriceCollectionRefused(Refusal):
    pass


class Collector:
    # Bound replay memory independently of the number of tokens/days read.
    MAX_PRICES = 32768
    MAX_TIMES = 16384
    MAX_ASSETS_PER_GROUP = 8

    def __init__(self, chain, directory, noah_netuid):
        self.chain = chain
        self.directory = Path(directory)
        self.netuid = noah_netuid
        self.genesis = chain.genesis_hash()
        self.prices = {}
        self.times = {}

    def _cache_entry(self, kind, key):
        identity = dict(genesis=self.genesis, kind=kind, key=key)
        path = self.directory / (digest(encoded(identity)) + ".json")
        return path, identity

    def _read_cached(self, path, identity):
        try:
            data = json.loads(path.read_text())
            if data["identity"] != identity or data["sha256"] != digest(encoded(data["value"])):
                if hasattr(self, 'eligibility'):
                    raise SourceLied("price cache identity/integrity failed")
                raise PriceCollectionRefused("price cache identity/integrity failed")
            return data["value"]
        except (ValueError, KeyError, TypeError) as exc:
            if hasattr(self, 'eligibility'):
                raise SourceLied("price cache structure is invalid") from exc
            raise

    def _cached(self, kind, key, read):
        path, identity = self._cache_entry(kind, key)
        if path.exists():
            return self._read_cached(path, identity)
        value = read()
        durable_write(
            path, encoded(dict(identity=identity, value=value, sha256=digest(encoded(value))))
        )
        return value

    def price(self, block, netuid):
        if netuid == 0:
            return R
        h = self.chain.block_hash(block)
        key = (block, h, netuid)
        if key not in self.prices:
            if len(self.prices) >= self.MAX_PRICES:
                self.prices.clear()
            read = self.chain.reference_price_at if hasattr(self, "eligibility") else self.chain.alpha_price_at
            self.prices[key] = read(block, netuid=netuid, expected_block_hash=h)
        return self.prices[key]

    def timestamp(self, block):
        h = self.chain.block_hash(block)
        key = (block, h)
        if key not in self.times:
            if len(self.times) >= self.MAX_TIMES:
                self.times.clear()
            self.times[key] = self.chain.block_timestamp(block)
        return self.times[key]

    def _window(self, block):
        head = self.chain.head_block()
        if block > head:
            raise PriceCollectionRefused("receipt is not finalized by both readers")
        timestamp = self.timestamp(block)
        end = timestamp // 86400 * 86400
        start = end - 86400

        # Bracket backwards from a recent finalized block. Genesis state may
        # legitimately be pruned even when the entire reference day is readable.
        # The stride is a search distance only, never an assumed block cadence.
        # Bracketing can need archive margin before the exact day. An unavailable
        # probe or source disagreement still refuses; neither is a time bound.
        def floor_at(target, upper):
            high = upper
            if self.timestamp(high) <= target:
                return high
            stride = 1
            while True:
                low = max(1, high - stride)
                if self.timestamp(low) <= target:
                    break
                if low == 1:
                    raise PriceCollectionRefused("prior UTC day predates readable chain")
                high = low
                stride *= 2
            while high - low > 1:
                mid = (low + high) // 2
                if self.timestamp(mid) <= target:
                    low = mid
                else:
                    high = mid
            if not self.timestamp(low) <= target < self.timestamp(high):
                raise PriceCollectionRefused("prior UTC day boundary changed during search")
            return low

        last = floor_at(end, block)
        first = floor_at(start, last)
        return [start, end, first, self.chain.block_hash(first), last, self.chain.block_hash(last)]

    def _collect_group(self, block, window, netuids):
        start, end, first, _, last, _ = window
        assert 0 < len(netuids) <= self.MAX_ASSETS_PER_GROUP
        rows = {netuid: [] for netuid in netuids}
        cursors = dict.fromkeys(netuids, start)
        for height in range(first, last + 1):
            for netuid in netuids:
                # Keep every existing timestamp/hash/canary/price call. Only
                # traversal order changes: the SDK's same-block setup stays hot.
                left = max(start, self.timestamp(height))
                right = min(end, self.timestamp(height + 1)) if height < block else end
                if right <= left:
                    continue
                if left != cursors[netuid]:
                    raise PriceCollectionRefused("noncontiguous price timeline")
                rows[netuid].append(
                    asdict(
                        PriceInterval(
                            left, right, self.price(height, self.netuid), self.price(height, netuid)
                        )
                    )
                )
                cursors[netuid] = right
            if all(cursor == end for cursor in cursors.values()):
                break
        for cursor in cursors.values():
            if cursor != end:
                raise PriceCollectionRefused("prior day price series incomplete")
        return rows

    def prior_day(self, block, netuid):
        window = self._window(block)
        if hasattr(self, "eligibility"):
            outcome = self.eligibility.result(self, block, netuid)
            if outcome["status"] != "supported":
                raise PriceCollectionRefused("reference is proven unsupported; use receipt disposition")
        key = [self.netuid, netuid, *window]
        rows = self._cached(
            "day", key, lambda: self._collect_group(block, window, [netuid])[netuid]
        )
        return [PriceInterval(**row) for row in rows]

    def precollect(self, block, netuids, *, expected_window=None, receipt_blocks=None):
        """Populate unchanged complete per-asset caches, at most eight at a time.

        The caller supplies the exact eligible new-receipt workload for one UTC
        reference day. Each asset retains the normal boundary/finality checks;
        price collection uses existing sequential, dual-source reader methods.
        No new file is written until every missing series in its group is complete.
        Earlier completed groups remain valid if a later group refuses.
        """
        supplied = tuple(netuids)
        if any(type(netuid) is not int or netuid < 0 for netuid in supplied):
            raise PriceCollectionRefused("invalid asset in price collection workload")
        assets = sorted(set(supplied))
        if hasattr(self, "eligibility"):
            for offset in range(0, len(assets), self.MAX_ASSETS_PER_GROUP):
                window = self._window(block)
                if expected_window is not None and tuple(window) != tuple(expected_window):
                    raise SourceLied("reference day changed since workload planning")
                self.eligibility.prove(self, block, assets[offset:offset + self.MAX_ASSETS_PER_GROUP], window,
                    receipt_blocks=None if receipt_blocks is None else {a: receipt_blocks[a] for a in assets[offset:offset + self.MAX_ASSETS_PER_GROUP]})
            return
        for offset in range(0, len(assets), self.MAX_ASSETS_PER_GROUP):
            missing = {}
            common = None
            for netuid in assets[offset : offset + self.MAX_ASSETS_PER_GROUP]:
                window = self._window(block)
                if expected_window is not None and tuple(window) != tuple(expected_window):
                    raise SourceLied("reference day changed since workload planning")
                if common is not None and window != common:
                    raise SourceLied("reference day changed during group preparation")
                common = window
                path, identity = self._cache_entry("day", [self.netuid, netuid, *window])
                if path.exists():
                    # Preserve cache integrity/shape refusal before considering
                    # the series available; never overwrite a broken cache.
                    [PriceInterval(**row) for row in self._read_cached(path, identity)]
                else:
                    missing[netuid] = (path, identity)
            if not missing:
                continue
            rows = self._collect_group(block, common, list(missing))
            for netuid, (path, identity) in missing.items():
                value = rows[netuid]
                durable_write(
                    path,
                    encoded(dict(identity=identity, value=value, sha256=digest(encoded(value)))),
                )
