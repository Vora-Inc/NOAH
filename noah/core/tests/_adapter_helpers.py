"""Shared synthetic inputs for public participant tests."""

from ...chain.store import DAY_SECONDS
from ...chain.tests._agreeing_helpers import (
    ANCHOR,
    ARK,
    NETUID,
    VALIDATOR_HOTKEY,
    a_gated_chain,
)
from ..chain_adapter import (
    ChainAdapter,
)

FLOORED_ANCHOR = ANCHOR // DAY_SECONDS * DAY_SECONDS
DIALS = {
    "targetFp": "500000000",
    "harshness": 1,
    "capFp": "1000000000",
    "halfLifeDays": "182",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "365",
    "ongoingHoldRao": "0",
}


class FakeReader:
    """Answers exactly the reads ChainAdapter delegates to a ChainReader."""

    def __init__(self, **answers):
        self.a = answers
        self.calls = []

    def current_block(self):
        self.calls.append("current_block")
        return self.a.get("current_block", 10000)

    def tempo(self, netuid, block=None):
        self.calls.append(("tempo", netuid, block))
        return self.a.get("tempo", 100)

    def committed_in_period(self, netuid, period, tempo, hotkey=None, head=None):
        self.calls.append(("committed_in_period", netuid, period, tempo, hotkey, head))
        by_period = self.a.get("committed_by_period")
        who = (
            by_period.get(period, set())
            if by_period is not None
            else self.a.get("committed", set())
        )
        return {h for h in who if hotkey is None or h == hotkey}

    def committed_in_block_range(self, netuid, from_block, to_block, hotkey=None, head=None):
        self.calls.append(("committed_in_block_range", netuid, from_block, to_block, hotkey, head))
        by_block = self.a.get("committed_by_block")
        if by_block is None:
            who = self.a.get("committed", set())
        else:
            last = to_block if head is None else min(to_block, head)
            who = set()
            for block, hotkeys in by_block.items():
                if from_block <= block <= last:
                    who |= set(hotkeys)
        return {h for h in who if hotkey is None or h == hotkey}

    def live_uids(self, netuid):
        self.calls.append(("live_uids", netuid))
        return self.a.get("live_uids", [0, 1])

    def owner_uids(self, netuid):
        self.calls.append(("owner_uids", netuid))
        return self.a.get("owner_uids", set())

    def last_update(self, uid, netuid):
        self.calls.append(("last_update", uid, netuid))
        return self.a.get("last_update", 0)

    def rate_limit(self, netuid):
        self.calls.append(("rate_limit", netuid))
        return self.a.get("rate_limit", 0)

    def weights_set_for_seat(self, netuid, uid, from_block, to_block):
        self.calls.append(("weights_set_for_seat", netuid, uid, from_block, to_block))
        answer = self.a.get("weights_set_for_seat", True)
        if callable(answer):
            return answer(from_block, to_block)
        return answer

    def weights_set_blocks_for_seat(self, netuid, uid, from_block, to_block):
        self.calls.append(("weights_set_blocks_for_seat", netuid, uid, from_block, to_block))
        known = self.a.get("weights_set_blocks")
        if known is None:
            return []
        if callable(known):
            return known(from_block, to_block)
        return sorted((b for b in known if from_block <= b <= to_block))

    def weights_content_for_seat(self, netuid, uid, block):
        self.calls.append(("weights_content_for_seat", netuid, uid, block))
        content_by_block = self.a.get("weights_content_by_block", {})
        return content_by_block.get(block, [])

    def close(self):
        self.calls.append("close")


class FakePublisher:
    """Answers exactly the reads/writes ChainAdapter delegates to a Publisher."""

    def __init__(self):
        self.sealed = []
        self.submitted = []
        self.stored = []
        self.direct_submitted = []

    def seal(self, uids, values, hotkey):
        commit = ("sealed", tuple(uids), tuple(values))
        self.sealed.append(commit)
        return (commit, 4242)

    def submit(self, commit, reveal_round, netuid):
        self.submitted.append((commit, reveal_round, netuid))
        self.stored.append(commit)
        return {"error": None, "block": 1}

    def read_map(self, name, netuid):
        from ...core.commits import LIVE_MAP

        return list(self.stored) if name == LIVE_MAP else []

    def submit_direct(self, uids, values, netuid):
        self.direct_submitted.append((tuple(uids), tuple(values), netuid))
        return {"error": None, "block": 1}


class TrivialHotkey:
    """The shape run_period's publish path needs: an ss58 address and a
    public key, without touching a real wallet or a real keypair."""

    def __init__(self, ss58_address="5FakeHotkey", public_key=b"\x01" * 32):
        self.ss58_address = ss58_address
        self.public_key = public_key


def an_adapter(**overrides):
    reader = overrides.pop("reader", FakeReader())
    publisher = overrides.pop("publisher", FakePublisher())
    runner_chain = overrides.pop("runner_chain", a_gated_chain())
    kwargs = dict(
        reader=reader,
        publisher=publisher,
        runner_chain=runner_chain,
        netuid=NETUID,
        uid=0,
        wallet_hotkey=TrivialHotkey(),
        settle_blocks=1,
    )
    kwargs.update(overrides)
    return (ChainAdapter(**kwargs), reader, publisher)


def _named_transfer(origin_coldkey, destination_coldkey, hotkey, netuid, amount):
    """A ``StakeAndHotkeyTransferred`` event's named fields: the one transfer
    event that names the miner hotkey (``origin_hotkey``) itself."""
    return {
        "origin_coldkey": origin_coldkey,
        "destination_coldkey": destination_coldkey,
        "origin_hotkey": hotkey,
        "destination_hotkey": hotkey,
        "origin_netuid": netuid,
        "destination_netuid": netuid,
        "amount": amount,
    }


__all__ = [
    "ARK",
    "DIALS",
    "FLOORED_ANCHOR",
    "FakePublisher",
    "FakeReader",
    "NETUID",
    "TrivialHotkey",
    "VALIDATOR_HOTKEY",
    "_named_transfer",
    "an_adapter",
]
