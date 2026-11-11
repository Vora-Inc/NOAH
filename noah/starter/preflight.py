"""Read-only two-reader migration preparation and startup verification; no wallet."""

import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ..chain.agreeing import AgreeingChain
from ..chain.dayblocks import day_anchor
from ..chain.history_check import confirm_history_contiguous
from ..chain.live import LiveRunnerChain
from ..chain.store import Store
from ..core.chain_adapter import _registration_days_for
from ..core.settings import Settings, parse_env_file
from ..core.version import version_key_for
from .allocation import POLICY_ID
from .entry import EntryPolicy
from .migration import freeze, save_new


class ReadOnlyHistory:
    def __init__(self, store, anchor):
        self.store, self.anchor = store, anchor

    def record_registration_block_day(self, block, timestamp):
        # A missing cache entry is derived from agreed chain time, never written.
        return (timestamp - self.anchor) // 86400

    def __getattr__(self, name):
        return getattr(self.store, name)


def seats_at(chain, block):
    hotkeys = chain.seat_hotkeys(block)
    owners = chain.hotkey_owners(list(hotkeys.values()), block)
    result = {}
    for uid, hot in hotkeys.items():
        result.setdefault(owners[hot], {})[hot] = uid
    return result


def inspect(chain, store, settings, snapshot=None):
    head = chain.head_block()
    genesis = chain.genesis_hash()
    expected = settings.mainnet_enable if settings.network == "finney" else settings.local_genesis
    if genesis != expected:
        raise ValueError("starter preflight chain genesis differs from configured target")
    anchor = store.day_anchor_ts
    day = (chain.block_timestamp(head) - anchor) // 86400
    info = chain.starter_epoch_info(head)
    first_day = (chain.block_timestamp(info["NetworkRegisteredAt"]) - anchor) // 86400
    if snapshot is None:
        cutoff = day - 1
        confirm_history_contiguous(store, through_day=cutoff, from_day=first_day)
        ranges = {d: high for d, low, high in store.scanned_days()}
        block = ranges[cutoff]
        registrations = _registration_days_for(chain, store, seats_at(chain, block), block)
        snapshot = freeze(
            store,
            registrations,
            genesis_hash=genesis,
            netuid=settings.netuid,
            finalized_block=block,
            finalized_hash=chain.block_hash(block),
            as_of_day=cutoff,
        )
    policy = EntryPolicy(snapshot)
    cutoff = snapshot["payload"]["asOfDay"]
    confirm_history_contiguous(store, through_day=cutoff, from_day=first_day)
    adapter = SimpleNamespace(
        runner_chain=chain,
        entry_deposit_rao=settings.entry_deposit_rao,
        netuid=settings.netuid,
        day_anchor_ts=anchor,
    )
    if policy.inputs(adapter, store, seats_at(chain, head), day, head) is None:
        raise ValueError("activation cutoff must precede the current finalized UTC day")
    return snapshot, dict(
        genesis=genesis,
        netuid=settings.netuid,
        head=head,
        cutoff_day=cutoff,
        snapshot_sha256=snapshot["sha256"],
        consensus=info["consensus"],
        version_floor=info["WeightsVersionKey"],
    )


def main():
    import os

    from async_substrate_interface import SubstrateInterface

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", type=Path)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--freeze", type=Path)
    choice.add_argument("--snapshot", type=Path)
    args = p.parse_args()
    mapping = (
        parse_env_file(args.env.read_text())
        if args.env
        else {k: v for k, v in os.environ.items() if k.startswith("NOAH_")}
    )
    settings = Settings.from_mapping(mapping)
    endpoints = {"primary": settings.chain_endpoint, "secondary": settings.second_chain_endpoint}
    clients = {}
    conn = None
    try:
        for name, endpoint in endpoints.items():
            clients[name] = LiveRunnerChain(
                SubstrateInterface(url=endpoint), netuid=settings.netuid, endpoint=endpoint
            )
        chain = AgreeingChain(clients, endpoints)
        conn = sqlite3.connect(Path(settings.history_db).resolve().as_uri() + "?mode=ro", uri=True)
        anchor = (day_anchor(chain) // 86400) * 86400
        store = ReadOnlyHistory(Store(conn, anchor), anchor)
        frozen, result = inspect(
            chain, store, settings, json.loads(args.snapshot.read_text()) if args.snapshot else None
        )
        if args.snapshot:
            if (
                result["consensus"] != "Yuma"
                or chain.mechanism_count(result["head"]) != 1
                or not settings.epoch_collection
                or not settings.epoch_republication
                or settings.publish_mode != "direct"
            ):
                raise ValueError(
                    "starter activation requires single-mechanism Yuma and direct epoch maintenance"
                )
            key = version_key_for("noah-scoring/surplus-bonus-v1/" + POLICY_ID)
            if settings.scoring_mode == "legacy" and key < result["version_floor"]:
                raise ValueError("starter publication version is below chain floor")
        else:
            save_new(args.freeze, frozen)
        print(
            json.dumps(
                dict(status="verified" if args.snapshot else "frozen", **result), sort_keys=True
            )
        )
    finally:
        if conn is not None:
            conn.close()
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    main()
