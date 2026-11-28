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


class _StakeMapSource:
    """Reuse one AlphaV2 scan for every subnet at the same endpoint and block.

    The validator collector keeps its own source. This wrapper is miner-only.
    """

    def __init__(self, client, cache, endpoint):
        self.client = client
        self._cache = cache
        self._endpoint = endpoint

    def call(self, method, params):
        return self.client.call(method, params)

    def alpha_inventory(self, netuid):
        from noah.reports.backing.reads import alpha_inventory
        key = (self._endpoint, self.client.block_hash)
        found = self._cache.get(key)
        if found is None:
            found = alpha_inventory(self, self.client.block_hash)
            self._cache[key] = found
        rows, positions, pages = found
        return list(rows.get(netuid, [])), pages, positions.get(netuid, 0)


def compute_report(config, contract, inventory=None):
    from noah.reports.backing.engine import compute
    from noah.reports.backing.rpc import RpcClient, close_sockets, websocket_transport
    from .backing.sdk_client import live_sdk

    connection = live_sdk(config.endpoint)
    try:
        check_snapshot(config, contract, connection)
    finally:
        connection.close()
    block = "0x" + contract["block_hash"]
    client = RpcClient([config.endpoint], block, websocket_transport)
    cache = {} if inventory is None else inventory
    try:
        result, _ = compute(_StakeMapSource(client, cache, config.endpoint), contract["reported_netuid"], block,
                            contract["block_number"], ark=bytes.fromhex(contract["ark_coldkey"]))
        return result
    finally:
        close_sockets()


def unready_reason(result):
    """Short reason code for a calculation that cannot be submitted.

    Only the engine's own code is returned. Endpoint text is never copied.
    """
    import re
    if not isinstance(result, dict):
        return "calculation_not_ready"
    if result.get("status") != "OK":
        reason = result.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]+", reason):
            return reason
        return "calculation_not_ready"
    for output in (result.get("output1"), result.get("output2"), result.get("output3")):
        if not isinstance(output, dict) or output.get("status") == "OK":
            continue
        reason = output.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]+", reason):
            return reason
        return "calculation_not_ready"
    return None


def prepare_report(config, contract, deposit_block=None, deposit_event=None, *, compute_fn=None, inventory=None):
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
            if compute_fn is not None:
                result = compute_fn(config, contract)
            else:
                result = compute_report(config, contract, inventory=inventory)
            reason = unready_reason(result)
            if reason is not None:
                raise ReportError(f"calculation is not ready ({reason})")
            try:
                calculation_for_shadow(contract, result)
            except ValueError as exc:
                text = str(exc)
                if "://" in text or "@" in text:
                    raise ReportError("calculation does not match the round") from None
                raise ReportError(text) from None
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


# One unlocked hotkey per process. A new process prompts again. The key is not written down.
_OPEN_HOTKEYS = {}


def _unlock_wallet(config):
    """Open the configured hotkey only. No mnemonic file or deposit coldkey."""
    from bittensor_wallet import Wallet
    kwargs = {"name": config.wallet_name, "hotkey": config.hotkey_name}
    if config.wallet_path:
        kwargs["path"] = config.wallet_path
    return Wallet(**kwargs).unlock_hotkey()


def wallet_signer(config):
    """Return the hotkey unlocked on the first new phase in this process."""
    key = (config.wallet_path or "", config.wallet_name, config.hotkey_name)
    cached = _OPEN_HOTKEYS.get(key)
    if cached is not None:
        return cached
    keypair = _unlock_wallet(config)
    _OPEN_HOTKEYS[key] = keypair
    return keypair


def submit_tick(config, contract, *, authorized=False, allow_mainnet=False, chain=None, signer_factory=None):
    from .proxy import sender_lane
    directory = state_directory(config, contract)
    with sender_lane(config, directory):
        return _submit_tick(config, contract, authorized=authorized, allow_mainnet=allow_mainnet,
                            chain=chain, signer_factory=signer_factory)


def _submit_tick(config, contract, *, authorized=False, allow_mainnet=False, chain=None, signer_factory=None):
    """Advance at most one phase. Safe to call repeatedly or after restart."""
    from noah.reports.backing.chain_session import SubstrateChain
    from noah.reports.backing.prepare import load_prepared
    from noah.reports.backing.round_config import preparation_from_round
    from noah.reports.backing.keys import ss58_decode
    from noah.reports.backing.submit import _refuse_wrong_chain, run_phase
    from substrateinterface import Keypair
    from .proxy import route, bind_route, claim

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
        chain.report_proxy = route(config)
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
            bind_route(config, directory, existing)
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
                chain.check_finalized_signing_context()
                from .backing.commitment_quota import required_space_for_payload
                payload = prepared.wire_commitment if phase == "commit" else prepared.submission.encode()
                quota = chain.commitment_quota(config.netuid, config.miner_hotkey,
                                               required_space_for_payload(payload))
                now = max(now, chain.finalized_time_ms())
                if quota is not None:
                    if quota["genesis_hash"].removeprefix("0x") != contract["genesis_hash"]:
                        raise ReportError("commitment quota belongs to another chain")
                    now = max(now, quota["finalized_timestamp_ms"])
                receipt = {"commitment_quota": quota} if quota is not None else {}
                if now >= closed:
                    return {**state, "action": "expired", "phase": phase, "sent": False, **receipt}
                if now < opened or quota is not None and not quota["allowed"]:
                    return {**state, "action": "waiting", "phase": phase, "sent": False,
                            "reason": "commitment_quota" if now >= opened else "phase_not_open", **receipt}
            # Existing journals can only reconcile. A public-only key is enough;
            # an unresolved attempt must not unlock signing material on restart.
            if not existing and chain.report_proxy and isinstance(chain, SubstrateChain):
                variant = 'Raw32' if phase == 'commit' and prepared.commitment_envelope is None else 'BigRaw'
                chain.preflight_proxy_report(config.netuid, variant,
                    prepared.wire_commitment if phase == 'commit' else prepared.submission.encode())
            signer_address = chain.report_proxy['delegate'] if chain.report_proxy else config.miner_hotkey
            keypair = Keypair(ss58_address=signer_address) if existing else (signer_factory or wallet_signer)(config)
            if keypair.public_key != ss58_decode(signer_address):
                raise ReportError("configured report wallet does not match the expected signer")
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

            def publish_sender():
                claim(config, directory, phase)

            result = run_phase(phase=phase, prepared_path=directory / "prepared.json",
                               announcement=announcement, mining_netuid=contract["mining_netuid"],
                               windows_doc=windows, keypair=keypair, evidence=directory, chain=chain,
                               now_ms=now if existing else None, round_windows=contract,
                               allow_mainnet=allow_mainnet, on_journal=publish_sender)
            return {**status(config, contract), "action": phase, "sent": result.sent,
                    **({"commitment_quota": quota} if quota is not None else {})}
        finally:
            if owned:
                chain.substrate.close()


def _roster_hash(document):
    import re
    if not isinstance(document, dict):
        raise ReportError("weekly assignment file is unreadable")
    value = document.get("roster_block_hash")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ReportError("weekly assignment file has no roster block")
    return value


def _missing_state(text):
    lowered = text.lower()
    return "unknown block" in lowered or "state discarded" in lowered or "state already discarded" in lowered


def _read_roster_block(chain, block_hash):
    pin = block_hash if str(block_hash).startswith("0x") else "0x" + block_hash
    try:
        response = chain.substrate.rpc_request("chain_getHeader", [pin])
    except Exception as exc:
        code = "block_state_unavailable" if _missing_state(str(exc)) else "rpc_transport"
        raise ReportError(f"roster block could not be read ({code})") from None
    error = response.get("error") if isinstance(response, dict) else None
    message = error.get("message", "") if isinstance(error, dict) else ""
    if not isinstance(response, dict) or error is not None or not isinstance(response.get("result"), dict):
        code = "block_state_unavailable" if _missing_state(str(message)) else "rpc_transport"
        raise ReportError(f"roster block could not be read ({code})") from None
    if "number" not in response["result"]:
        raise ReportError("roster block could not be read (rpc_transport)")
    return {"state": "readable", "block_hash": pin.removeprefix("0x")}


def _current_roster_hash(config, chain):
    """Return the Sunday roster hash, or None when this week's file is not here."""
    if not getattr(config, "assignments_url", None):
        return None
    from .assignments import current_document
    from .schedule_runner import assignment_identity
    try:
        key, identity = assignment_identity(config)
        document = current_document(config.state_dir, config.assignments_url, chain.finalized_time_ms() // 1000,
                                    trusted_public_key=key, expected_template_identity=identity)
    except ReportError:
        return None
    if document is None:
        return None
    return _roster_hash(document)


def preflight(config, *, chain=None, roster_block_hash=None, lookup_assignments=True):
    """Read the endpoint, runtime, and Sunday roster block. No wallet and no transaction."""
    from .backing.chain_session import SubstrateChain
    from .backing.rpc import endpoint_label
    owned = chain is None
    chain = chain or SubstrateChain(config.endpoint)
    try:
        genesis = str(chain.genesis()).lower().removeprefix("0x")
        expected = (getattr(config, "genesis_hash", None) or "").lower().removeprefix("0x")
        if expected and genesis != expected:
            raise ReportError("report endpoint has the wrong genesis")
        chain.ensure_supported_signing_runtime()
        if roster_block_hash is None and lookup_assignments:
            roster_block_hash = _current_roster_hash(config, chain)
        if roster_block_hash is None:
            roster = {"state": "file_unavailable" if getattr(config, "assignments_url", None) else "not_configured"}
        else:
            roster = _read_roster_block(chain, roster_block_hash)
        return {"action": "preflight", "sent": False, "signed": False,
                "endpoint": endpoint_label(config.endpoint or ""),
                "genesis_ok": True, "runtime": "supported", "roster_block": roster}
    finally:
        if owned:
            chain.substrate.close()
