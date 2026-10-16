"""``python -m noah.deposits.assistant`` : preview, run-once, status, init-config.

The real chain connection and wallet are built here, lazily, so everything else
stays importable and testable without them. The wallet is opened only after the
miner types the confirmation; this module never reads or writes recovery words.
Pause, resume, edit and stop are for scheduled runs, which this first version
does not have: there is nothing running to pause.
"""

import argparse
import os
import sys
import threading

from ...chain.sources import Refusal, connect_scrubbed, endpoint_label
from ...pathtext import plain_path_text
from ..live_chain_source import LiveChainSource
from .config import EXAMPLE, STAKE_POSITION_HELP, WRAPPED_DEPOSIT_NOTICE, load_config
from .evidence import JsonFileEvidence
from .ledger import Ledger
from .preview import render_status
from .session import _check_chain_identity as check_chain_identity
from .session import Services, preview, reconcile_open, resolve_credit, run_once


class ConnectionFailed(ConnectionError):
    """The chain connection could not be opened; the text never carries the endpoint URL."""


def build_services(config):
    """Real connection, source and signer factory. Imports the chain client lazily."""
    from async_substrate_interface import SubstrateInterface

    connection = connect_scrubbed(
        lambda: SubstrateInterface(url=config.endpoint, auto_discover=False),
        config.endpoint,
        ConnectionFailed,
    )
    try:
        return _services_on(connection, config, LiveChainSource)
    except BaseException:
        _close_quietly(connection)
        raise


def _services_on(connection, config, LiveSource):
    def signer_factory():
        from .proxy_signing import is_proxy, reject
        if is_proxy(config):
            reject('proxy keys are only available to the isolated signer')
        from bittensor import Wallet

        kwargs = {"name": config.wallet_name, "hotkey": config.hotkey_name}
        if config.wallet_path:
            kwargs["path"] = config.wallet_path
        return Wallet(**kwargs).coldkey  # the wallet's own prompt unlocks it if encrypted

    os.makedirs(config.state_dir, exist_ok=True)
    return Services(
        connection=connection,
        source=LiveSource(connection, endpoint=config.endpoint),
        evidence_provider=JsonFileEvidence(config.evidence_file),
        ledger=Ledger(os.path.join(config.state_dir, "deposits.db")),
        signer_factory=signer_factory,
    )


def _parser():
    from ...chain.sources import QuietArgumentParser

    p = QuietArgumentParser(
        prog="noah-deposit",
        description="Guided Ark deposits, signed by you.",
        epilog=WRAPPED_DEPOSIT_NOTICE + "\n\n" + STAKE_POSITION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="noah-deposit.ini")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-config", help="write an example settings file (never overwrites)")
    preview_parser = sub.add_parser("preview", help="calculate and explain; sends nothing")
    preview_parser.add_argument("--label", help="workflow intent label; direct previews create no intent")
    r = sub.add_parser("run-once", help="preview, ask you to type a confirmation, then send")
    r.add_argument(
        "--label", help="custom/fixed modes: names this deposit so a deliberate repeat is allowed"
    )
    sub.add_parser(
        "status",
        help="show where each deposit stands, checking the chain",
        description="Show where each deposit stands. Credit confirmation compares credited "
        "totals, so an unrelated credit of the same size could confirm early; a manual deposit "
        "in the same window can confuse a check (the tool never re-sends by itself).",
    )
    rs = sub.add_parser(
        "resolve",
        help="supply the credited total measured before a deposit whose credit is not tracked",
    )
    rs.add_argument("intent_id", help="the deposit id (or its first characters) shown by status")
    rs.add_argument(
        "--credited-before-rao",
        type=int,
        required=True,
        help="credited total in rao just before that deposit, from the published records",
    )
    return p


def _close_quietly(connection):
    """Close the chain connection if it can be closed; never raise from here."""
    close = getattr(connection, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass


def finish(code):
    """Exit with ``code`` promptly, even if a library left a non-daemon thread running.

    The chain client can leave a thread alive after its work is done, which keeps the
    interpreter from exiting. By this point all output is printed and every ledger write
    is committed, so flush and leave without waiting for those threads.
    """
    code = 0 if code is None else code
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    lingering = [
        t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon
    ]
    if lingering:
        os._exit(code if isinstance(code, int) else 1)
    sys.exit(code)


def main(argv=None, services_factory=None):
    if services_factory is None:
        services_factory = build_services
    args = _parser().parse_args(argv)
    if args.command == "init-config":
        shown = plain_path_text(args.config)
        if os.path.exists(args.config):
            print(f"{shown} already exists; not overwriting.")
            return 1
        try:
            with open(args.config, "w", encoding="utf-8") as handle:
                handle.write(EXAMPLE)
        except OSError as error:
            print(f"Could not write the settings file ({type(error).__name__}).")
            return 1
        print(
            f"Wrote {shown}. Edit it, then run: preview\n"
            f"Note: the endpoint (default ws://127.0.0.1:9944) is the chain node this tool "
            f"reads from, here a node on this computer. Point it at a node you run or trust. "
            f"Set genesis_hash in the file so the tool refuses to run on the wrong network.\n\n"
            f"{WRAPPED_DEPOSIT_NOTICE}\n\n{STAKE_POSITION_HELP}"
        )
        return 0
    config = None
    try:
        config = load_config(args.config)
        return _run(args, config, services_factory)
    except Refusal as refusal:
        print(f"Stopped.\n  Why: {refusal}")
        return 2
    except Exception as failure:
        if config is None or not _is_connection_failure(failure):
            raise
        print(
            f"Stopped.\n  Why: cannot reach the chain node at {endpoint_label(config.endpoint)} "
            f"({type(failure).__name__}).\n"
            f"  Check that the node is running, that the endpoint in your settings file is right "
            f"(ws://host:port), and that your network allows the connection.\n"
            f"  If a deposit was in progress, it is recorded safely; run `status` once the node is "
            f"reachable to see where it stands."
        )
        return 2


def _is_connection_failure(failure):
    if isinstance(failure, (OSError, TimeoutError)):  # includes ConnectionError
        return True
    module = type(failure).__module__ or ""
    name = type(failure).__name__
    return module.startswith(("websockets", "async_substrate_interface", "ssl")) or any(
        word in name for word in ("Connection", "Timeout")
    )


def _run(args, config, services_factory):
    services = services_factory(config)
    try:
        return _dispatch(args, config, services)
    finally:
        _close_quietly(services.connection)


def _dispatch(args, config, services):
    if args.command == "preview":
        result = preview(config, services)
        return 0 if result["status"] != "refused" else 2
    if args.command == "run-once":
        result = run_once(config, services, label=args.label)
        return (
            0
            if result["status"]
            in (
                "finalized",
                "credited",
                "submitted",
                "nothing_due",
                "below_minimum",
                "declined",
            )
            else 2
        )
    if args.command == "resolve":
        result = resolve_credit(config, services, args.intent_id, args.credited_before_rao)
        return 0 if result["status"] != "declined" else 2
    check_chain_identity(config, services)
    reconcile_open(config, services)
    print(
        render_status(
            config,
            services.ledger.last_calculation(config.miner_hotkey),
            services.ledger.for_miner(config.miner_hotkey),
        )
    )
    return 0


if __name__ == "__main__":
    finish(main())
