"""Run the validator after explicit chain, identity and publication preflight checks.

A dry run reads configuration and chain state without loading a signing key or
publishing weights. A normal run opens the configured signer and schedules settled
periods through the chain adapter. Missing history, identity or configuration is
reported as a refusal. Passing startup checks does not prove later publication or
prior-period verification succeeded; inspect the individual period results."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from noah import secretscrub  # noqa: E402
from noah.diagnostics import configure as configure_logging, write as diagnostic, run_entrypoint  # noqa: E402
from noah.chain.sources import (  # noqa: E402
    connect_scrubbed,
    endpoint_label,
    guard_endpoint_logging,
    scrub_endpoint_text,
)
from noah.core.chain import WrongChain, identify, require_chain  # noqa: E402
from noah.pathtext import plain_path_text  # noqa: E402
from noah.core.mainnet import check_signer_hotkey, clear_mainnet  # noqa: E402
from noah.core.proxy_route import commit_address  # noqa: E402
from noah.core.chain_adapter import build_chain_adapter  # noqa: E402
from noah.runtime_events import bind as bind_event_context, emit as emit_event, traced  # noqa: E402
from noah.core.loop import run_forever  # noqa: E402
from noah.core.pinning import NoSettledPeriod, period_bounds, period_of, pin_height  # noqa: E402
from noah.core.reader import ChainReader, ChainUnreadable, Connection  # noqa: E402
from noah.core.settings import Settings, SettingsError, parse_env_file  # noqa: E402
from noah.core.startup import (  # noqa: E402
    CannotPublishYet,
    DirectPathMisconfigured,
    NotArchiveCapable,
    NotRegistered,
    NoValidatorPermit,
    SealedPathUnavailable,
    SubnetDoesNotExist,
    UnsupportedLibraryVersion,
    check_can_publish_yet,
    check_commit_reveal_enabled,
    check_has_validator_permit,
    check_library_versions,
    check_reads_history,
    check_registered,
    check_subnet_exists,
)

# How far back the node must still answer a read of chain state. Scoring reads
# the newest completed days from the chain; everything older is already in
# history.db, which is permanent. A mainnet node keeps about 90 days of state,
# so the requirement is a few days, not an archive. The old value, 300 blocks
# (one hour), let a node that kept an hour of state pass while every day this
# validator has to read is at least 7,200 blocks (twelve-second blocks) long.
# Three days covers the day being scored, the day before it, and a restart
# spanning both. A longer outage is not this check's to prove, and this probe
# cannot see history.db at all. What stops a store with missing days from being
# scored is noah.chain.history_check.require_whole_history, which
# noah.chain.fixture_build.build_fixture runs before it builds anything: from
# the day history.db itself records the subnet began (its emission start, or
# its earliest registration) through the day being scored, every day must have
# a recorded scan, or scoring refuses and names the gap. It does not check that
# the block ranges of those days join up; scripts/check_history_contiguity.py
# does, and an operator runs it on a restored copy before trusting it.
BLOCKS_PER_DAY = 7_200
HISTORY_DEPTH_DAYS = 3
HISTORY_DEPTH = HISTORY_DEPTH_DAYS * BLOCKS_PER_DAY


def _default_state_dir():
    """Resolve durable loop state outside disposable code checkouts when configured.

    An explicit state directory takes precedence; otherwise use the runtime root or
    the supported checkout fallback. Retain existing paths across restarts so progress
    and publication guards are not silently reset."""
    override = os.environ.get("NOAH_RUNTIME_ROOT", "").strip()
    if override:
        return Path(override) / "ticket-14" / "state"
    checkout_root = Path(__file__).resolve().parents[3]
    return checkout_root.parent / "state"


def _commit_ledger_path(state_dir):
    """Resolve the per-day commit ledger path independently of loop progress.

    The environment override takes precedence over the state-directory default."""
    override = os.environ.get("NOAH_COMMIT_LEDGER_PATH", "").strip()
    if override:
        return Path(override)
    return Path(state_dir) / "commit-ledger.json"


def _submitted_record_dir():
    """Resolve the directory for per-day submitted-vector evidence."""
    override = os.environ.get("NOAH_RUNTIME_ROOT", "").strip()
    if override:
        return Path(override) / "ticket-16" / "scores"
    return Path(__file__).resolve().parents[3].parent.parent / "runtime" / "ticket-16" / "scores"


def _publish_intent_path(state_dir):
    """Resolve the durable send-intent marker path; the environment may override it."""
    override = os.environ.get("NOAH_PUBLISH_INTENT_PATH", "").strip()
    if override:
        return Path(override)
    return Path(state_dir) / "publish-intent.json"


def _publish_backoff_path(state_dir):
    """The proxy route's persisted failed-send count (``noah.core.proxy_route``), beside the
    publish-intent marker. ``NOAH_PUBLISH_BACKOFF_PATH`` overrides it."""
    override = os.environ.get("NOAH_PUBLISH_BACKOFF_PATH", "").strip()
    if override:
        return Path(override)
    return Path(state_dir) / "publish-backoff.json"


def _publish_hold_path(state_dir):
    """Resolve the operator publication-hold path; the environment may override it."""
    override = os.environ.get("NOAH_PUBLISH_HOLD_PATH", "").strip()
    if override:
        return Path(override)
    return Path(state_dir) / "publish-hold.json"


def load_settings(env_path):
    """Settings from a file if one is named, otherwise from the environment."""
    if env_path:
        text = Path(env_path).read_text()
        return Settings.from_mapping(parse_env_file(text))
    return Settings.from_mapping(
        {key: value for key, value in os.environ.items() if key.startswith("NOAH_")}
    )


def _open_bt_client(endpoint):
    import bittensor as bt

    return connect_scrubbed(lambda: bt.subtensor(network=endpoint), endpoint, SettingsError)


def _close_quietly(client):
    closer = getattr(client, "close", None)
    if closer is not None:
        try:
            closer()
        except Exception:
            pass


def _load_wallet(settings):
    """The bittensor wallet named by the settings. The only place a signing key is loaded."""
    import bittensor as bt

    return bt.wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey)


def connect(settings, *, open_client=None, env=None, genesis_connection=None):
    """Open a client, and refuse a chain that is not the one asked for.

    Mainnet is refused unless ``noah.core.mainnet.clear_mainnet`` clears it: every
    setting condition, and BOTH read endpoints (and the submit endpoint, if one is
    set) reporting the mainnet genesis. With any condition unmet the refusal is the
    same ``WrongChain`` this function always raised for mainnet, now naming each gap.
    ``open_client``, ``env`` and ``genesis_connection`` (endpoint -> object with
    ``genesis_hash()``) exist so tests can inject fake chains and profiles. On mainnet
    each endpoint is identified with a state-free ``chain_getBlockHash(0)`` (see
    ``noah.core.mainnet.RpcGenesis``), so a pruned own node can be an endpoint.
    """
    open_client = open_client or _open_bt_client
    endpoint = settings.chain_endpoint or settings.network
    expected = {"finney": "MAINNET", "test": "TESTNET", "local": "LOCAL"}[settings.network]
    if expected != "MAINNET":
        client = open_client(endpoint)
        genesis = require_chain(Connection(client), expected, local_genesis=settings.local_genesis)
        return client, genesis

    from noah.core.mainnet import refusal, setting_problems

    early = setting_problems(settings, env)
    if early:
        # Refuse before opening any connection; every unmet setting is named.
        raise refusal(early)
    from noah.core.mainnet import RpcGenesis

    genesis_connection = genesis_connection or RpcGenesis
    connections = [
        (name, ep, genesis_connection(ep))
        for name, ep in (
            ("NOAH_CHAIN_ENDPOINT", settings.chain_endpoint),
            ("NOAH_ENDPOINT_2", settings.second_chain_endpoint),
            ("NOAH_SUBMIT_ENDPOINT", settings.submit_endpoint),
        )
        if ep
    ]
    # Every endpoint's genesis is read BEFORE the validator's own client is opened.
    clearance = clear_mainnet(settings, connections, env)
    client = open_client(endpoint)
    genesis = require_chain(connections[0][2], expected, clearance=clearance)
    return client, genesis


def open_submit_client(settings, genesis, *, open_client=None, genesis_connection=None):
    """The client the signed commit is submitted through, or None to use the read client.

    Only when ``NOAH_SUBMIT_ENDPOINT`` is set. Its genesis must equal the genesis the
    read client was identified as, so a submission can never go to a different chain
    than the one that was read.
    """
    submit_endpoint = getattr(settings, "submit_endpoint", None)
    if not isinstance(submit_endpoint, str) or not submit_endpoint:
        return None
    from noah.core.mainnet import RpcGenesis

    genesis_connection = genesis_connection or RpcGenesis
    client = (open_client or _open_bt_client)(submit_endpoint)
    # chain_getBlockHash(0), state-free, as for the read endpoints on mainnet.
    answered, _ = identify(genesis_connection(submit_endpoint))
    if answered != genesis:
        _close_quietly(client)
        raise WrongChain(
            f"NOAH_SUBMIT_ENDPOINT reports genesis {answered}, but the validator was "
            f"identified against {genesis}. Refusing to submit to a different chain."
        )
    return client


def _path_status(path):
    """exists / writable / absent-as-expected, without creating anything."""
    path = Path(path)
    if path.exists():
        return f"exists, {'writable' if os.access(path, os.W_OK) else 'NOT WRITABLE'}"
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK):
        return "absent (expected before the first real start); can be created"
    return f"absent and NOT creatable ({plain_path_text(parent)} is not writable)"


def mainnet_preflight(settings, reader, genesis, hotkey, *, env=None, genesis_connection=None, state_dir=None, wallet_doc=None):
    """The one read-only report a mainnet dry run prints after the gate.

    Everything is a read of configuration, files or the chain. Nothing is created,
    signed or sent. Returns the number of lines that are NOT ok.
    """
    import hashlib
    import re

    from noah.core import mainnet as mn
    from noah.core.mainnet import RpcGenesis
    from noah.core.chain import MAINNET
    from noah.scoring.parameters import ParametersError, load_parameters

    env = os.environ if env is None else env
    genesis_connection = genesis_connection or RpcGenesis
    bad = 0

    def line(label, ok, text):
        nonlocal bad
        bad += 0 if ok else 1
        diagnostic(f"  {label:<26} {'ok  ' if ok else 'FAIL'} {text}", severity="info" if ok else "error")

    print("Mainnet preflight (read-only):")
    doc_addresses = None
    doc_path = wallet_doc
    if doc_path is None:
        for parent in Path(__file__).resolve().parents:
            if (parent / "docs/reference/WALLET_ADDRESSES.md").is_file():
                doc_path = parent / "docs/reference/WALLET_ADDRESSES.md"
                break
    if doc_path and Path(doc_path).is_file():
        doc_addresses = set(re.findall(r"`(5[1-9A-HJ-NP-Za-km-z]{47})`", Path(doc_path).read_text()))
    for label, value, expected in (
        ("scoring hotkey (public)", hotkey, mn.SIGNER_HOTKEY),
        ("Ark Validator hotkey", settings.designated_validator_hotkey, mn.ARK_VALIDATOR_HOTKEY),
        ("Ark address", settings.ark_address, mn.ARK_ADDRESS),
    ):
        in_doc = "WALLET_ADDRESSES.md not found" if doc_addresses is None else (
            "matches WALLET_ADDRESSES.md" if value in doc_addresses else "NOT in WALLET_ADDRESSES.md"
        )
        line(label, value == expected and (doc_addresses is None or value in doc_addresses), f"{value}  ({in_doc})")

    seen = {}
    for name, ep in (
        ("NOAH_CHAIN_ENDPOINT", settings.chain_endpoint),
        ("NOAH_ENDPOINT_2", settings.second_chain_endpoint),
        ("NOAH_SUBMIT_ENDPOINT", settings.submit_endpoint),
    ):
        if not ep:
            continue
        try:
            answered = genesis_connection(ep).genesis_hash()
        except SettingsError as error:
            answered = f"unanswered ({scrub_endpoint_text(error, ep)})"
        seen[name] = answered
        role = {
            "NOAH_CHAIN_ENDPOINT": "reads: the gate and every period read go through it"
            + ("" if settings.submit_endpoint else "; also SUBMITS the signed commit"),
            "NOAH_ENDPOINT_2": "reads: second, independent source the two-source agreement checks against",
            "NOAH_SUBMIT_ENDPOINT": "SUBMITS the signed commit only",
        }[name]
        line(name, answered == MAINNET, f"{endpoint_label(ep)}  genesis {answered}  [{role}]")
    from noah.chain.sources import canonical_endpoint

    reads = {canonical_endpoint(settings.chain_endpoint), canonical_endpoint(settings.second_chain_endpoint)}
    line("independent read endpoints", len(reads) == 2, f"{len(reads)} distinct")

    try:
        params = load_parameters(env)
        path = env.get("NOAH_PARAMETERS_JSON", "")
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        ok = (
            params.profile_version.startswith(mn.PROFILE_PREFIX)
            and params.target_fp == 500_000_000
            and params.entry_deposit_per_hotkey_rao == 10_000_000_000
            and settings.entry_deposit_rao == params.entry_deposit_per_hotkey_rao
        )
        line(
            "profile",
            ok,
            f"{params.profile_version}  sha256 {digest}  target {params.target_fp / 1e7:g}%  "
            f"entry {params.entry_deposit_per_hotkey_rao / 1e9:g} NOAH  hold {params.ongoing_hold_rao}",
        )
    except ParametersError as error:
        line("profile", False, str(error))

    try:
        probe = reader.historical_probe(HISTORY_DEPTH)
        head = reader.current_block()
        line(
            "required history",
            bool(probe),
            f"{HISTORY_DEPTH_DAYS} days = {HISTORY_DEPTH:,} blocks; probe Tempo[0] at block "
            f"{head - HISTORY_DEPTH:,} read {probe!r}",
        )
    except SettingsError as error:
        line("required history", False, f"probe failed: {error}")

    line(
        "publication mode",
        settings.publish_mode in ("sealed", "direct"),
        f"{settings.publish_mode} (configured proxy route; chain mode must match)",
    )

    state_dir = Path(state_dir) if state_dir is not None else _default_state_dir()
    ledger = _commit_ledger_path(state_dir)
    from noah.core.chain_adapter import _default_validator_history_db_path

    history = settings.history_db or _default_validator_history_db_path(settings.netuid, settings.ark_address)
    for label, path in (
        ("state directory", state_dir),
        ("loop state (loop.json)", state_dir / "loop.json"),
        ("loop lock (loop.lock)", state_dir / "loop.lock"),
        ("commit ledger", ledger),
        ("history db", history),
    ):
        status = _path_status(path)
        line(label, "NOT" not in status, f"{plain_path_text(path)}: {status}")
    print(f"  signing                    none: dry run, no key loaded, nothing sent\n")
    return bad


@traced("validator", "validator.startup")
def gate(reader, settings, hotkey):
    """The four questions a validator must answer before its first period.

    Returned as a list of (question, outcome) rather than raised one at a time,
    so a run reports everything it found. Stopping at the first refusal tells an
    operator to fix one thing, and they fix it and meet the next one.

    Every check below catches its own named refusal AND ``ChainUnreadable``.
    Each one reads the chain to answer its question, and a read that fails
    outright -- a dropped connection, a timeout -- is a fact about this
    attempt, not about the seat. It gets the same clean, reported refusal as a
    real "no" rather than escaping as a raw traceback that reads like this
    validator itself is broken.
    """
    results = []

    # First, because almost every other read answers from a default and a
    # subnet that was never created reports an ordinary configuration.
    try:
        check_subnet_exists(reader, settings.netuid)
        results.append(("subnet exists", "yes"))
    except (SubnetDoesNotExist, ChainUnreadable) as error:
        results.append(("subnet exists", f"NO. {error}"))
        return results, None

    try:
        uid = check_registered(reader, hotkey, settings.netuid)
        results.append(("holds a seat", f"yes, seat {uid}"))
        emit_event("validator", "registration.observed", "validator.startup",
                   netuid=settings.netuid, uid=uid, validator_hotkey=hotkey, outcome="registered")
    except (NotRegistered, ChainUnreadable) as error:
        results.append(("holds a seat", f"NO. {error}"))
        return results, None

    if settings.publish_mode == "direct":
        sealed_label = "sealed publication off (direct mode)"
        sealed_refusal = (SealedPathUnavailable, DirectPathMisconfigured)
    else:
        sealed_label = "sealed publication on"
        sealed_refusal = SealedPathUnavailable

    for label, call, refusal in (
        (
            "may publish yet",
            lambda: check_can_publish_yet(reader, uid, settings.netuid),
            CannotPublishYet,
        ),
        # After the rate limit, because a seat can pass every other question and
        # still have its weights thrown away at reveal.
        (
            "weights are accepted",
            lambda: check_has_validator_permit(reader, uid, settings.netuid),
            NoValidatorPermit,
        ),
        (
            "node answers history",
            lambda: check_reads_history(reader, HISTORY_DEPTH),
            NotArchiveCapable,
        ),
        (
            sealed_label,
            lambda: check_commit_reveal_enabled(reader, settings.netuid, settings.publish_mode),
            sealed_refusal,
        ),
    ):
        # refusal is a single exception class for every check except the
        # sealed-publication one, which is a tuple in "direct" mode (it can
        # refuse two distinct ways -- see sealed_refusal above). `except`
        # does not flatten a nested tuple of tuples on its own (confirmed:
        # `except ((A, B), C)` raises TypeError, it does not catch B), so this
        # builds one flat tuple either way rather than relying on that.
        refusals = refusal if isinstance(refusal, tuple) else (refusal,)
        try:
            call()
            results.append((label, "yes"))
        except (*refusals, ChainUnreadable) as error:
            results.append((label, f"NO. {error}"))
    # Proxy route only (empty otherwise): the delegation from the validator to the dedicated
    # signer must exist as NonFungible with delay 0, and the signer must hold fee money.
    from noah.core.proxy_route import gate_lines

    results.extend(gate_lines(reader, settings))
    return results, uid


def survey(reader, settings, hotkey):
    """Read and report the state a period would use, without signing or publishing."""
    try:
        tempo = reader.tempo(settings.netuid)
        head = reader.current_block()
    except ChainUnreadable as error:
        diagnostic(f"  could not read what a period would act on. {error}", severity="warning")
        return False
    try:
        pinned = pin_height(head=head, tempo=tempo, settle_blocks=settings.settle_blocks)
    except NoSettledPeriod as error:
        print(f"  nothing to score yet\n    {error}")
        return True

    period = period_of(height=pinned, tempo=tempo)
    first, last = period_bounds(period=period, tempo=tempo)
    print(f"  period length         {tempo} blocks")
    print(f"  would score           period {period}, blocks {first:,} to {last:,}")
    print(f"  pinned height         {pinned:,}, settled {head - pinned} blocks ago")

    if settings.publish_mode == "direct":
        # Sealed commits cannot establish direct-publication status. LastUpdate
        # is only seat activity (including registration), not a day receipt.
        try:
            uid = check_registered(reader, hotkey, settings.netuid)
            last_update = reader.last_update(uid, settings.netuid)
        except (NotRegistered, ChainUnreadable) as error:
            diagnostic(f"  seat last update      could not tell. {error}", severity="warning")
            return False
        print(f"  seat last update      block {last_update:,} (seat {uid})")
        print("    activity only; not proof that a scoring day was published")
        print("  direct status         not evaluated by this survey")
        print("    real runs verify durable receipt/intent and finalized-chain reconciliation")
    else:
        try:
            committed = reader.committed_in_period(settings.netuid, period, tempo, head=head)
        except ChainUnreadable as error:
            diagnostic(f"  already published     could not tell. {error}", severity="warning")
            return False
        ours = hotkey in committed
        if ours:
            print("  already published     YES, by us; a real run would refuse and keep its window")
        elif committed:
            print(f"  already published     by {len(committed)} other validator(s), not by us")
        else:
            print("  already published     no")

    try:
        live = reader.live_uids(settings.netuid)
        owner = reader.owner_uids(settings.netuid)
    except ChainUnreadable as error:
        diagnostic(f"  could not read the seat list. {error}", severity="warning")
        return False
    print(f"  seats to name         {len(live)}")
    if owner:
        print(f"  seats we must not pay {sorted(owner)}, which the keeper refuses to weight")
    else:
        print("  seats we must not pay none")
    return True


@traced("validator", "validator.run", fresh=True)
def main(argv=None):
    guard_endpoint_logging()
    try:
        check_library_versions()
    except UnsupportedLibraryVersion as error:
        diagnostic(f"ENVIRONMENT REFUSED\n  {error}", severity="error")
        return 2

    from noah.chain.sources import QuietArgumentParser

    parser = QuietArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", help="a settings file; otherwise the environment is read")
    parser.add_argument(
        "--dry-run", action="store_true", help="read and check everything, sign nothing"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env)
        secretscrub.activate(settings)
        configure_logging(settings.log_level)
        guard_endpoint_logging()
    except SettingsError as error:
        diagnostic(f"SETTINGS REFUSED\n  {scrub_endpoint_text(error)}", severity="error")
        return 2

    print(f"network        {settings.network}, subnet {settings.netuid}")
    # Labels only (scheme://host[:port]): a hosted RPC URL may carry an API key.
    primary = endpoint_label(settings.chain_endpoint) if settings.chain_endpoint else None
    print(f"endpoint       {primary or '(the network default)'}")
    if settings.second_chain_endpoint:
        print(f"endpoint 2     {endpoint_label(settings.second_chain_endpoint)}")
    if settings.submit_endpoint:
        print(f"submit via     {endpoint_label(settings.submit_endpoint)}")
    print(f"wallet         {settings.wallet_name} / {settings.wallet_hotkey}")

    urls = (settings.chain_endpoint, settings.second_chain_endpoint, settings.submit_endpoint)
    try:
        client, genesis = connect(settings)
        reader = ChainReader(client)
        height = reader.current_block()
    except SettingsError as error:
        diagnostic(f"\nCHAIN REFUSED\n  {scrub_endpoint_text(error, *urls)}", severity="error")
        return 2

    bind_event_context("validator", genesis_hash=genesis, netuid=settings.netuid)
    print(f"chain          {genesis}")
    print(f"height         {height:,}\n")

    expected_signer = getattr(settings, "expected_signer_address", None)
    public_only = bool(args.dry_run and isinstance(expected_signer, str) and expected_signer)
    wallet = None
    if public_only:
        hotkey = expected_signer
        print(f"hotkey         {hotkey}  (public address; no key loaded, dry run)\n")
    else:
        wallet = _load_wallet(settings)
        loaded_hotkey = wallet.hotkey.ss58_address
        try:
            check_signer_hotkey(settings, loaded_hotkey)  # mainnet, or whenever an address is configured
            # The address every validator question is asked about: the validator's own
            # (commit identity) on the proxy route, the loaded hotkey otherwise.
            hotkey = commit_address(settings, loaded_hotkey)
        except SettingsError as error:
            diagnostic(f"SIGNER REFUSED\n  {error}", severity="error")
            return 2
        if hotkey != loaded_hotkey:
            print(f"validator      {hotkey}  (commit identity; its key is NOT loaded)")
            print(f"proxy signer   {loaded_hotkey}  (signs, pays the fee)\n")
        else:
            print(f"hotkey         {hotkey}\n")
    results, uid = gate(reader, settings, hotkey)
    width = max(len(label) for label, _ in results)
    for label, outcome in results:
        first, *rest = outcome.split(". ", 1)
        diagnostic(f"  {label:<{width}}  {first}", severity="error" if outcome.startswith("NO") else "info")
        for line in rest:
            for chunk in line.split(". "):
                diagnostic(f"  {'':<{width}}  {chunk.strip()}", severity="error" if outcome.startswith("NO") else "info")
    passed = all(not outcome.startswith("NO") for _, outcome in results)

    print()
    if not passed:
        diagnostic("THE GATE REFUSED. This validator will not start, which is the point:", severity="error")
        print("each line above stops it before a period rather than during one.")
        if args.dry_run and uid is not None:
            print("\nWhat it would have acted on, since some of these refusals pass:\n")
            survey(reader, settings, hotkey)
        return 1

    if args.dry_run:
        print("THE GATE PASSED and this is a dry run, so nothing was signed.")
        if settings.network == "finney":
            print()
            if mainnet_preflight(settings, reader, genesis, hotkey, env=os.environ):
                diagnostic("MAINNET PREFLIGHT HAS FAILING LINES (see FAIL above).", severity="error")
                return 1
        print("What a real run would act on, all of it read and none of it done:\n")
        if not survey(reader, settings, hotkey):
            return 2
        return 0

    try:
        submit_client = open_submit_client(settings, genesis)
    except SettingsError as error:
        diagnostic(f"\nSUBMIT ENDPOINT REFUSED\n  {scrub_endpoint_text(error, *urls)}", severity="error")
        return 3

    print("THE GATE PASSED. Building the real chain adapter (#436) before starting the")
    print("unattended loop.")
    try:
        adapter = build_chain_adapter(
            client=client,
            settings=settings,
            reader=reader,
            wallet=wallet,
            uid=uid,
            read_collateral=True,
            commit_ledger_path=_commit_ledger_path(_default_state_dir()),
            publish_intent_path=_publish_intent_path(_default_state_dir()),
            publish_hold_path=_publish_hold_path(_default_state_dir()),
            publish_backoff_path=_publish_backoff_path(_default_state_dir()),
            submitted_record_dir=_submitted_record_dir(),
            submit_client=submit_client,
        )
    except SettingsError as error:
        # SingleSourceChainRefused (no NOAH_ENDPOINT_2 configured) reaches
        # here today on every real deployment: this project's chain reads
        # are never single-sourced, and the milestone chain currently
        # exposes one endpoint. Reported the same clean way every other
        # startup refusal in this file is, rather than as a raw traceback.
        diagnostic(f"\nCHAIN ADAPTER REFUSED\n  {scrub_endpoint_text(error, *urls)}", severity="error")
        return 3

    print("Adapter built. Starting the unattended loop (noah.core.loop.run_forever).")
    print("The loop reads each eligible settled period and reports any named refusal;")
    print("a passing period may submit weights through the configured publication route.")
    state_dir = _default_state_dir()
    run_forever(
        adapter,
        settle_blocks=settings.settle_blocks,
        state_path=str(state_dir / "loop.json"),
        lock_path=str(state_dir / "loop.lock"),
        identity=f"{genesis}/{settings.netuid}/{hotkey}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_entrypoint(main))
