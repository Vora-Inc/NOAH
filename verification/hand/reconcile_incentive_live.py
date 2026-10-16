#!/usr/bin/env python3
"""Compare emitted incentive distributions with historical Incentive storage.

Reuse the agreed chain reader for events, seat identities and storage at the same
block. Search a specified range for a pay event. The storage reader currently
reads mechanism zero; reports for multiple mechanisms require separate review.
This diagnostic does not publish weights or submit deposits."""

from __future__ import annotations

import argparse
import re
import json
import os
import sys
from pathlib import Path

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[2]
_HAND_DIR = Path(__file__).resolve().parent
for _p in (str(_SOFTWARE_NOAH_ROOT), str(_HAND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import compare_incentive  # noqa: E402  (sibling module, path extended above)

from noah.chain.emissions import (  # noqa: E402
    epoch_pay_vectors,
    pay_event_present,
)


class _QuietParser(argparse.ArgumentParser):
    """argparse repeats rejected text in its errors ("unrecognized arguments: <text>", "ignored
    explicit argument '<text>'", a type function's message, "invalid choice: '<text>'"), and an
    operator may have typed a keyed endpoint URL into the wrong place. While parsing, every error
    becomes a fixed line built from the option NAME only, never from typed text; usage goes to
    stderr and the exit is 2 as argparse does. Argparse's own required / missing-value messages
    carry no typed text and stay as they are, and a script's own fixed errors raised after parsing
    pass through."""

    _SAFE = re.compile(
        r"^(the following arguments are required: [^'\"]*|expected (one|at least one|\d+) arguments?)$"
    )

    def __init__(self, *args, fixed_messages=frozenset(), **kwargs):
        # fixed_messages: constant texts of this script's own type= checks that carry no typed
        # value; only an exact match is shown, anything else is replaced by the fixed line.
        kwargs.setdefault("exit_on_error", False)
        super().__init__(*args, **kwargs)
        self._quiet_parsing = 0
        self._fixed_messages = frozenset(fixed_messages)

    def _passes(self, message):
        prefixed = re.match(r"^argument [^:]+: (.*)$", message, re.S)
        text = prefixed.group(1) if prefixed else message
        return bool(self._SAFE.match(text)) or text in self._fixed_messages

    def _guarded(self, parse, args, namespace):
        # Every rejection ends in one fixed line, whichever argparse version raises it where:
        # 3.9 to 3.11 call error() from inside parse_known_args, 3.12+ raise ArgumentError
        # (3.13+ also for "unrecognized arguments", after parse_known_args has returned).
        self._quiet_parsing += 1
        try:
            return parse(args, namespace)
        except argparse.ArgumentError as rejected:
            message = rejected.message or ""
            name = rejected.argument_name
            shown = f"argument {name}: {message}" if name else message
            if self._passes(shown):
                self._fail(shown)
            if name:
                self._fail(f"argument {name}: value rejected (values are not echoed)")
            self._fail("invalid or unrecognized arguments (values are not echoed)")
        finally:
            self._quiet_parsing -= 1

    def parse_args(self, args=None, namespace=None):
        return self._guarded(super(_QuietParser, self).parse_args, args, namespace)

    def parse_known_args(self, args=None, namespace=None):
        return self._guarded(super(_QuietParser, self).parse_known_args, args, namespace)

    def _fail(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")

    def error(self, message):
        if not self._quiet_parsing:
            self._fail(message)
        if self._passes(message):
            self._fail(message)
        self._fail("invalid or unrecognized arguments (values are not echoed)")


class NoEpochBlockFound(Exception):
    """No block in the given range paid this subnet at all."""


def find_real_epoch_block(runner_chain, netuid, from_block, to_block):
    """The first real block in ``[from_block, to_block]`` that paid
    ``netuid`` -- or raise. Every block in the range is checked; a block
    that could not be read raises rather than being silently skipped
    (whatever ``runner_chain.events`` itself raises propagates unchanged),
    the same completeness guarantee ``noah.chain.runner.run_day`` already
    has by construction.
    """
    for block in range(from_block, to_block + 1):
        events = runner_chain.events(block)
        if pay_event_present(events, netuid=netuid):
            return block, events
    raise NoEpochBlockFound(
        f"no block from {from_block} to {to_block} paid netuid {netuid}. Every block in "
        f"the range was read (a read failure would have raised, not been skipped); none "
        f"named this subnet in an IncentiveAlphaEmittedToMiners event."
    )


def read_real_incentive_perU16(runner_chain, block):
    """Real, dual-sourced ``SubtensorModule::Incentive`` storage at this
    subnet's own index, at ``block`` -- ``AgreeingChain.incentive(block)``,
    canary-checked and cross-source-agreed the same as every other single-
    block fact ``run_day`` itself reads. Returns ``{seat: perU16_value}``.

    Packs on ``self.netuid`` alone (mechanism 0), matching
    ``LiveRunnerChain.incentive``'s own documented, named limitation for a
    subnet running more than one mechanism -- see that method's own
    docstring, not repeated here.
    """
    vector = runner_chain.incentive(block)
    return {seat: int(v) for seat, v in enumerate(vector)}


def reconcile_live(runner_chain, *, netuid, block, events):
    """Real event side + real, dual-sourced storage side, reconciled, for
    one real epoch block. Returns ``compare_incentive.reconcile``'s own
    report dict, with ``block`` and ``mechanism_reports`` (one
    reconciliation per mechanism that actually paid, since a subnet may
    run more than one) added.
    """
    hotkey_by_uid = runner_chain.seat_hotkeys(block)
    subnetwork_n = runner_chain.subnetwork_n(block)
    mechanism_count = runner_chain.mechanism_count(block)

    vectors = epoch_pay_vectors(
        events,
        netuid=netuid,
        mechanism_count=mechanism_count,
        hotkey_by_uid=hotkey_by_uid,
        subnetwork_n=subnetwork_n,
        block=block,
    )

    incentive_perU16_by_seat = read_real_incentive_perU16(runner_chain, block)
    mechanism_reports = {}
    for mechanism, event_amounts_by_seat in vectors.items():
        # Mechanism 0 only today (see this module's own docstring); every
        # mechanism reconciles against the same single-mechanism read until
        # incentive() itself is packed-index-aware.
        mechanism_reports[mechanism] = compare_incentive.reconcile(
            event_amounts_by_seat, incentive_perU16_by_seat
        )

    overall_agree = (
        all(r["agree"] for r in mechanism_reports.values()) if mechanism_reports else None
    )
    return {
        "block": block,
        "netuid": netuid,
        "mechanisms_paid": sorted(mechanism_reports),
        "agree": overall_agree,
        "mechanism_reports": mechanism_reports,
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = _QuietParser(description=__doc__)
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument("--out", type=str, default=None, help="write the report JSON here too")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(_SOFTWARE_NOAH_ROOT))
    from noah.core.settings import KEYS as SETTINGS_KEYS
    from noah.core.settings import Settings

    mapping = {k: v for k, v in os.environ.items() if k in SETTINGS_KEYS}
    mapping.setdefault("NOAH_NETUID", str(args.netuid))
    settings = Settings.from_mapping(mapping)

    from neurons.validator import connect

    client, genesis = connect(settings)
    import bittensor as bt

    from noah.core.chain_adapter import build_chain_adapter
    from noah.core.reader import ChainReader

    reader = ChainReader(client)
    wallet = bt.wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey)
    adapter = None
    try:
        adapter = build_chain_adapter(
            client=client,
            settings=settings,
            reader=reader,
            wallet=wallet,
            uid=None,
            read_collateral=False,
        )
        block, events = find_real_epoch_block(
            adapter.runner_chain, args.netuid, args.from_block, args.to_block
        )
        print(f"real epoch block found: {block}")
        report = reconcile_live(
            adapter.runner_chain, netuid=args.netuid, block=block, events=events
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if report["agree"] is None:
            return 2
        return 0 if report["agree"] else 1
    except NoEpochBlockFound as error:
        print(f"NO EPOCH BLOCK: {error}")
        return 2
    finally:
        (adapter or client).close()


if __name__ == "__main__":
    raise SystemExit(main())
