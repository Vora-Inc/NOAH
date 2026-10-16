"""Inspect chain receipts for unresolved deposit intents.

Finalized transfer evidence and credited scoring evidence are different states.
Uncertain submission remains subject to the configured reconciliation window;
matching and chain-identity policy must be reviewed independently."""

from dataclasses import dataclass

from ...chain.deposits import deposits_in_block
from ...chain.sources import Refusal


class ChainUnreadable(Refusal):
    pass


class _OwnerSource:
    def __init__(self, connection):
        self._connection = connection

    def owner_at(self, *, hotkey, block_hash):
        result = self._connection.query("SubtensorModule", "Owner", [hotkey], block_hash=block_hash)
        return getattr(result, "value", result)


def _events(raw, block):
    if not isinstance(raw, (list, tuple)):
        raise ChainUnreadable(f"the events of block {block} came back as {raw!r}, not a list.")
    return [dict(record, ordinal_in_block=i) for i, record in enumerate(raw)]


def _extrinsics(raw):
    if not isinstance(raw, (list, tuple)):
        raise ChainUnreadable(f"a block's extrinsics came back as {raw!r}, not a list.")
    return [getattr(record, "value", record) for record in raw]


@dataclass(frozen=True)
class Match:
    deposit: object
    qualifies: bool
    reason: str = None


def deposits_in(connection, *, ark_coldkey, validator_hotkey, block):
    block_hash = connection.get_block_hash(block)
    parent = connection.get_block_hash(block - 1) if block > 0 else block_hash
    events = _events(connection.get_events(block_hash=block_hash), block)
    extrinsics = _extrinsics(connection.get_block(block_hash=block_hash)["extrinsics"])
    return deposits_in_block(
        events,
        ark_coldkey,
        block,
        block_hash,
        validator_hotkey,
        extrinsics=extrinsics,
        parent_block_hash=parent,
        owner_source=_OwnerSource(connection),
    )


def find_deposit(
    connection,
    *,
    ark_coldkey,
    validator_hotkey,
    coldkey,
    miner_hotkey,
    expected_netuid,
    amount_rao,
    from_block,
    to_block,
):
    """The deposit in ``[from_block, to_block]`` this intent was for: from this coldkey, tied by
    the reader to this miner, attributed by the reader, AND of the intended token and amount. If none fits,
    the first other deposit of ours in the window is returned with ``qualifies=False`` and the
    reason (wrong validator, unattributed, or a different amount), never silently accepted.
    ``None`` when nothing of ours is in the window.

    Limit (N3): matching is by coldkey and miner hotkey inside the window, so a manual deposit
    made in the same window can be taken for this intent or flagged. This fails safe: it never
    leads to a second send."""
    flagged = None
    for block in range(from_block, to_block + 1):
        for d in deposits_in(
            connection, ark_coldkey=ark_coldkey, validator_hotkey=validator_hotkey, block=block
        ):
            # An unattributed deposit from our own coldkey inside this intent's block range is
            # ours too: money moved, and it must not be mistaken for "never sent".
            if d.coldkey != coldkey or d.origin_hotkey not in (miner_hotkey, None):
                continue
            if d.token_netuid != expected_netuid:
                if flagged is None:
                    flagged = Match(d, False, f"landed subnet {d.token_netuid} alpha, intended subnet {expected_netuid}")
                continue
            if d.unattributed_reason is None and d.amount_rao == amount_rao:
                return Match(d, True)  # the deposit this intent was for
            if flagged is None:
                reason = d.unattributed_reason or (
                    f"landed {d.amount_rao} rao, intended {amount_rao} rao"
                )
                flagged = Match(d, False, reason)
    return flagged


def head_height(connection):
    return connection.get_block_number(connection.get_chain_head())


def finalized_height(connection):
    return connection.get_block_number(connection.get_chain_finalised_head())
