"""One durable report, advanced by a command or a periodic scheduler tick.

Only submit_tick can load a signer. Waiting, status and preparation do not.
An uncertain send is reconciled by the original journal, never resent.
"""

from .common import ReportError, immutable_json, locked, read_json, state_directory


def check_snapshot(config, contract, connection):
    from .backing.keys import ss58_decode
    if str(connection.get_block_hash(0)).removeprefix("0x") != contract["genesis_hash"]:
        raise ReportError("report endpoint has the wrong genesis")
    head = connection.rpc_request("chain_getFinalizedHead", [])["result"]
    number = connection.get_block_header(head)["header"]["number"]
    number = int(number, 16) if isinstance(number, str) else number
    block = "0x" + contract["block_hash"]
    if number < contract["block_number"] or str(connection.get_block_hash(contract["block_number"])) != block:
        raise ReportError("snapshot B is not on the endpoint's finalized chain")
    timestamp = connection.query("Timestamp", "Now", block_hash=block).value
    if int(timestamp) != contract["block_timestamp_ms"]:
        raise ReportError("snapshot timestamp differs from the trusted round")
    uid = connection.query("SubtensorModule", "Uids", [config.netuid, config.miner_hotkey], block_hash=block).value
    owner = connection.query("SubtensorModule", "Owner", [config.miner_hotkey], block_hash=block).value
    if uid is None or ss58_decode(owner) != ss58_decode(config.coldkey):
        raise ReportError("miner registration or owner differs at snapshot B")


def compute_report(config, contract):
    from noah.reports.backing.engine import compute
    from noah.reports.backing.rpc import RpcClient, close_sockets, websocket_transport
    from noah.reports.backing.source import LiveSource
    from .backing.sdk_client import live_sdk

    connection = live_sdk(config.endpoint)
    try:
        check_snapshot(config, contract, connection)
    finally:
        connection.close()
    block = "0x" + contract["block_hash"]
    client = RpcClient([config.endpoint], block, websocket_transport)
    try:
        result, _ = compute(LiveSource(client), contract["reported_netuid"], block,
                            contract["block_number"], ark=bytes.fromhex(contract["ark_coldkey"]))
        return result
    finally:
        close_sockets()


def prepare_report(config, contract, deposit_block=None, deposit_event=None, *, compute_fn=None):
    from noah.reports.backing.keys import ss58_decode
    from noah.reports.backing.prepare import prepare, load_prepared
    from noah.reports.backing.round_config import preparation_from_round, calculation_for_shadow

    from .backing.protocol.preimage import REPORT_POLICY
    report_only = contract["eligibility_policy_version"] == REPORT_POLICY
    if report_only and (deposit_block is not None or deposit_event is not None):
        raise ReportError("report-only preparation does not accept deposit references")
    if not report_only and (type(deposit_block) is not int or type(deposit_event) is not int
            or not 0 <= deposit_block < contract["block_number"]
            or not 0 <= deposit_event < 2**16):
        raise ReportError("cite a deposit before snapshot B and a valid block-wide event index")
    directory = state_directory(config, contract)
    with locked(directory):
        immutable_json(directory / "round.json", contract)
        request = {"deposit_block": deposit_block, "deposit_event": deposit_event,
                   "coldkey": config.coldkey, "hotkey": config.miner_hotkey}
        immutable_json(directory / "request.json", request)
        path = directory / "prepared.json"
        if not path.exists():
            result = (compute_fn or compute_report)(config, contract)
            calculation_for_shadow(contract, result)
            if result.get("block_timestamp_ms") != contract["block_timestamp_ms"]:
                raise ReportError("calculation timestamp differs from the trusted round")
            ten = result["output2"]["alpha_required_rao"]
            twenty = result["output3"]["alpha_required_rao"]
            if (not 0 <= result["output1"]["drop_ppm"] <= 1_000_000
                    or ten <= 0 or twenty < ten or ten % 1_000_000 or twenty % 1_000_000):
                raise ReportError("calculation does not meet the report's integer output rules")
            args = preparation_from_round(
                contract, hotkey_hex=ss58_decode(config.miner_hotkey).hex(),
                coldkey_hex=ss58_decode(config.coldkey).hex(), deposit_block=deposit_block,
                deposit_event=deposit_event, endpoint=config.endpoint,
            )
            prepare(**args, out=path, compute_fn=lambda *_: result)
        prepared = load_prepared(path)
        return {**status(config, contract), "outputs": {
            "drop_ppm": prepared.submission.out1_ppm,
            "ten_percent_rao": str(prepared.submission.out2_rao),
            "twenty_percent_rao": str(prepared.submission.out3_rao),
        }, **({"report_policy": REPORT_POLICY} if report_only else {
            "deposit_reference": "miner-supplied; validator checks chain history"})}


def status(config, contract):
    directory = state_directory(config, contract)
    result = {"round": directory.name, "prepared": (directory / "prepared.json").is_file(),
              "commit": "not_submitted", "reveal": "not_submitted", "directory": str(directory)}
    for phase in ("commit", "reveal"):
        path = directory / f"{phase}.journal"
        if path.exists():
            journal = read_json(path)
            result[phase] = journal["state"]
            result[f"{phase}_receipt"] = journal.get("locator")
    return result


def wallet_signer(config):
    """Open the configured hotkey only. No mnemonic file or deposit coldkey."""
    from bittensor_wallet import Wallet
    kwargs = {"name": config.wallet_name, "hotkey": config.hotkey_name}
    if config.wallet_path:
        kwargs["path"] = config.wallet_path
    return Wallet(**kwargs).hotkey


def submit_tick(config, contract, *, authorized=False, allow_mainnet=False, chain=None, signer_factory=None):
    """Advance at most one phase. Safe to call repeatedly or after restart."""
    from noah.reports.backing.chain_session import SubstrateChain
    from noah.reports.backing.prepare import load_prepared
    from noah.reports.backing.round_config import preparation_from_round
    from noah.reports.backing.keys import ss58_decode
    from noah.reports.backing.submit import _refuse_wrong_chain, run_phase
    from substrateinterface import Keypair

    directory = state_directory(config, contract)
    # CommitmentOf has one slot per miner/subnet. Serialize sends. A new
    # commit waits until the previous commit journal is finalized. The
    # reveal may follow later: selection matches each reveal to its hash.
    with locked(directory.parent), locked(directory):
        if read_json(directory / "round.json") != contract:
            raise ReportError("saved round differs from trusted round")
        prepared = load_prepared(directory / "prepared.json")
        if (prepared.context_round is not None and prepared.context_round != contract
                or "commitment_format" in contract and prepared.context_round is None):
            raise ReportError("prepared commitment format or round differs from trusted round")
        if (prepared.submission.hotkey != ss58_decode(config.miner_hotkey)
                or prepared.submission.coldkey != ss58_decode(config.coldkey)):
            raise ReportError("prepared report belongs to another miner")
        owned = chain is None
        chain = chain or SubstrateChain(config.endpoint)
        if isinstance(chain, SubstrateChain):
            chain.search_from_block = contract["block_number"]
        try:
            _refuse_wrong_chain(chain.genesis(), {"genesis": contract["genesis_hash"]}, allow_mainnet=allow_mainnet)
            if allow_mainnet:
                from .backing.protocol.preimage import ReportSubmission, REPORT_POLICY
                if (not isinstance(prepared.submission, ReportSubmission) or config.netuid != 82
                        or contract['eligibility_policy_version'] != REPORT_POLICY):
                    raise ReportError("Mainnet opt-in requires report-only policy on mining subnet82")
            state = status(config, contract)
            now = chain.finalized_time_ms()
            if state["reveal"] == "finalized":
                return {**state, "action": "complete", "sent": False}
            phase = "commit" if state["commit"] != "finalized" else "reveal"
            opened, closed = contract[f"{phase}_open_ms"], contract[f"{phase}_close_ms"]
            existing = (directory / f"{phase}.journal").exists()
            if not existing and now < opened:
                return {**state, "action": "waiting", "phase": phase, "next_at_ms": opened, "sent": False}
            if not existing and now >= closed:
                return {**state, "action": "expired", "phase": phase, "sent": False}
            if not existing and not authorized:
                raise ReportError("submission requires confirmation; use --yes for an authorized scheduled run")
            if not existing:
                for prior in directory.parent.glob("*/commit.journal"):
                    if prior.parent == directory:
                        continue
                    reveal = prior.parent / "reveal.journal"
                    prior_commit = read_json(prior).get("state")
                    prior_reveal = read_json(reveal).get("state") if reveal.is_file() else None
                    safe_to_move = (prior_commit == "failed" or prior_reveal in ("finalized", "failed")
                        or (prior_commit == "finalized" and prior_reveal is None))
                    if not safe_to_move:
                        raise ReportError("another round has an unfinished commitment; reconcile it before submitting")
            quota = None
            if not existing and isinstance(chain, SubstrateChain):
                # Preparation pins B, but a scheduled phase may sign hours later.
                # Refuse a newly unsupported runtime before opening the wallet.
                chain.ensure_supported_signing_runtime()
                from .backing.commitment_quota import required_space_for_payload
                payload = prepared.wire_commitment if phase == "commit" else prepared.submission.encode()
                quota = chain.commitment_quota(config.netuid, config.miner_hotkey,
                                               required_space_for_payload(payload))
                if quota["genesis_hash"].removeprefix("0x") != contract["genesis_hash"]:
                    raise ReportError("commitment quota belongs to another chain")
                # Quota storage reads may be slow after their head was pinned.
                # Check the current clock after those reads; retain the receipt
                # unchanged, and never move time backwards or extend the windows.
                now = max(now, quota["finalized_timestamp_ms"], chain.finalized_time_ms())
                if now >= closed:
                    return {**state, "action": "expired", "phase": phase, "sent": False,
                            "commitment_quota": quota}
                if now < opened or not quota["allowed"]:
                    return {**state, "action": "waiting", "phase": phase, "sent": False,
                            "reason": "commitment_quota" if now >= opened else "phase_not_open",
                            "commitment_quota": quota}
            # Existing journals can only reconcile. A public-only key is enough;
            # an unresolved attempt must not unlock signing material on restart.
            keypair = Keypair(ss58_address=config.miner_hotkey) if existing else (signer_factory or wallet_signer)(config)
            args = preparation_from_round(
                contract, hotkey_hex=ss58_decode(config.miner_hotkey).hex(),
                coldkey_hex=ss58_decode(config.coldkey).hex(),
                deposit_block=getattr(prepared.submission, "deposit_block", None),
                deposit_event=getattr(prepared.submission, "deposit_event", None), endpoint=config.endpoint,
            )
            # run_phase uses the exact explicit product windows; no implicit
            # change of deadline on retries. The adapter supports a gap too.
            announcement = {**args["announcement"], "announced_at_ms": contract["commit_open_ms"]}
            windows = {"commit_window_ms": contract["commit_close_ms"] - contract["commit_open_ms"],
                       "reveal_window_ms": contract["reveal_close_ms"] - contract["reveal_open_ms"],
                       "accelerated": True, "clock": "operator-round"}
            result = run_phase(phase=phase, prepared_path=directory / "prepared.json",
                               announcement=announcement, mining_netuid=contract["mining_netuid"],
                               windows_doc=windows, keypair=keypair, evidence=directory, chain=chain,
                               now_ms=now if existing else None, round_windows=contract,
                               allow_mainnet=allow_mainnet)
            return {**status(config, contract), "action": phase, "sent": result.sent,
                    **({"commitment_quota": quota} if quota is not None else {})}
        finally:
            if owned:
                chain.substrate.close()
