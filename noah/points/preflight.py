"""Read-only points-mode startup verification using both finalized readers.

Reads the selected immutable policy, history anchor, outstanding intent and
starter proof. Does not load a wallet, install policy, price receipts or sign.
"""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ..chain.agreeing import AgreeingChain
from ..chain.dayblocks import day_anchor
from ..chain.entry_history import EntryPolicy
from ..chain.live import LiveRunnerChain
from ..core.settings import Settings, parse_env_file
from .onboarding import MODE as ORDINARY_MODE
from .runtime import configure


def inspect(chain, settings, runtime_root, *, amendment_transition=False):
    genesis = chain.genesis_hash()
    expected = settings.mainnet_enable if settings.network == "finney" else settings.local_genesis
    if genesis != expected:
        raise ValueError("points preflight chain genesis differs from configured target")
    root = Path(runtime_root)
    pending = root / "ticket-14/state/publish-intent.json"
    adapter = SimpleNamespace(
        runner_chain=chain,
        netuid=settings.netuid,
        day_anchor_ts=(day_anchor(chain) // 86400) * 86400,
        ark_coldkey=settings.ark_address,
        designated_validator_hotkey=settings.designated_validator_hotkey,
        epoch_collection=settings.epoch_collection,
        epoch_republication=settings.epoch_republication,
        starter_entry_policy=None
        if settings.scoring_mode == ORDINARY_MODE
        else EntryPolicy.load(settings.starter_entry_snapshot),
        entry_history_policy=EntryPolicy.load(settings.starter_entry_snapshot)
        if settings.scoring_mode == ORDINARY_MODE
        else None,
        _submitted_record_dir=root / "ticket-16/scores",
        _publish_intent=SimpleNamespace(
            load=lambda: json.loads(pending.read_text()) if pending.exists() else None
        ),
        publisher=SimpleNamespace(),
    )
    configure(adapter, settings, persist=False, amendment_transition=amendment_transition)
    return dict(
        status="verified",
        read_only=True,
        genesis=genesis,
        netuid=settings.netuid,
        mode=adapter.scoring_mode,
        publication_version=adapter.publisher.version_key,
        policy=adapter.points_policy.manifest,
    )


def main():
    from async_substrate_interface import SubstrateInterface

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--amendment-transition", action="store_true")
    args = parser.parse_args()
    mapping = (
        parse_env_file(args.env.read_text())
        if args.env
        else {k: v for k, v in os.environ.items() if k.startswith("NOAH_")}
    )
    settings = Settings.from_mapping(mapping)
    if args.env:
        # Select the supplied configuration for canonical environment readers.
        # Parameter loading remains solely in scoring.parameters.
        os.environ.update(mapping)
    endpoints = {"primary": settings.chain_endpoint, "secondary": settings.second_chain_endpoint}
    clients = {}
    try:
        for name, endpoint in endpoints.items():
            clients[name] = LiveRunnerChain(
                SubstrateInterface(url=endpoint), netuid=settings.netuid, endpoint=endpoint
            )
        print(
            json.dumps(
                inspect(
                    AgreeingChain(clients, endpoints),
                    settings,
                    mapping["NOAH_RUNTIME_ROOT"],
                    amendment_transition=args.amendment_transition,
                ),
                sort_keys=True,
            )
        )
    finally:
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    main()
