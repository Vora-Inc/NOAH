"""Collect one round from a node, using that node's runtime metadata.

``RpcClient`` is failover, not agreement. ``substrate-interface`` decodes
extrinsics and events with the metadata the connected node serves. A faulty
endpoint can still lie. Retained blocks and metadata let a later replay see
the same responses. They do not prove the endpoint was honest.

Coverage is complete only when the finalized head's timestamp is at or after
``reveal_close`` and the parent walk from that head is continuous back through
``commit_open``. The observed timestamp is what is recorded. The requested
deadline is not written as if it had been seen.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from substrateinterface import SubstrateInterface
from .sdk_client import live_sdk

from noah.chain.deposits import NotADepositEvent, deposits_in_block
from noah.chain.sources import Refusal
from noah.reports.evaluation import prove_evaluation

from .engine import compute
from .finality import prove_ancestry
from .history import checkpoint_problem, make_checkpoint, prefix_landed, segments_problem
from .keys import blake2_128_concat, hex_encode, netuid_le, ss58_decode, ss58_encode, storage_prefix
from .round_config import calculation_for_shadow, require_round
from .rpc import RpcClient, result_sha256, websocket_transport, endpoint_label, close_sockets
from .select import select_for_miner
from .source import LiveSource, save_receipts

COLLECTOR_ID = "noah.reports.backing.collector"


def uids_key(netuid: int, hotkey: bytes) -> bytes:
    """Identity netuid and Blake2_128Concat hotkey, as spec 490 metadata states."""
    return storage_prefix("SubtensorModule", "Uids") + netuid_le(netuid) + blake2_128_concat(hotkey)


def owner_key(hotkey: bytes) -> bytes:
    return storage_prefix("SubtensorModule", "Owner") + blake2_128_concat(hotkey)


def collect(round_contract: dict, urls: list[str], out_dir: Path, hotkeys: list[str], *, clock=time.time, history_checkpoint=None, prior_responses=None, history_dir=None, evaluation=None, evaluation_ancestor_limit=None, budget=None, reader_label=None) -> dict:
    require_round(round_contract)
    if not urls:
        raise ValueError("no endpoints")
    if reader_label not in (None, 'reader_a', 'reader_b'):
        raise ValueError('invalid private reader label')
    from noah.reports.budget import ProviderBudget
    from .protocol.preimage import REPORT_POLICY
    owned_budget = budget is None and round_contract['eligibility_policy_version'] == REPORT_POLICY
    if owned_budget:
        budget = ProviderBudget()
    substrate = None
    try:
        substrate, endpoint = _connect(urls, budget=budget)
        return _collect(
            substrate,
            reader_label or endpoint,
            urls,
            round_contract,
            Path(out_dir),
            hotkeys,
            clock,
            history_checkpoint=history_checkpoint,
            prior_responses=prior_responses,
            history_dir=history_dir,
            evaluation=evaluation,
            evaluation_ancestor_limit=evaluation_ancestor_limit,
            budget=budget,
        )
    finally:
        # A broken SDK close must not leave the independent budget transports
        # alive. Preserve the original collection failure while closing each.
        cleanup = [close_sockets]
        if substrate is not None:
            cleanup.append(substrate.close)
        if owned_budget:
            cleanup.append(budget.close)
        for close in cleanup:
            try:
                close()
            except Exception:
                pass


def _collect(substrate, endpoint, urls, round_contract, out_dir, hotkeys, clock, *, chain_source=None, history_checkpoint=None, prior_responses=None, history_dir=None, evaluation=None, evaluation_ancestor_limit=None, budget=None, shared_window=None, capture_window=False, shared_engine=None, mining_netuid_filter=None):
    from .protocol.preimage import REPORT_POLICY
    report_only = round_contract["eligibility_policy_version"] == REPORT_POLICY
    if report_only and any(value is not None for value in (history_checkpoint, prior_responses, history_dir)):
        raise ValueError("report-only collection does not accept deposit history")
    if capture_window:
        from ..shared_window import window_binding
        if not report_only or any(v is not None for v in (history_checkpoint, prior_responses, history_dir)):
            raise ValueError('shared window requires report-only collection')
        binding = window_binding(round_contract, evaluation, evaluation_ancestor_limit)
    elif shared_window is not None:
        raise ValueError('shared window requires explicit collection mode')
    if shared_window is None:
        raw_metadata = substrate.rpc_request("state_getMetadata", [])["result"]
        metadata_sha = hashlib.sha256(bytes.fromhex(raw_metadata[2:])).hexdigest()
        version = substrate.rpc_request("state_getRuntimeVersion", [])["result"]
        evaluation_proof = None
        if evaluation is not None:
            from noah.reports.adjudication_contract import require
            evaluation_proof = prove_evaluation(
                _SubstrateSource(substrate), evaluation,
                genesis_hash=round_contract["genesis_hash"],
                max_ancestor_distance=evaluation_ancestor_limit,
            )
            require(evaluation["timestamp_ms"] >= round_contract["reveal_close_ms"]
                    and evaluation["block_number"] >= round_contract["block_number"],
                    "evaluation must close the required round")
            head = "0x" + evaluation["block_hash"]
        else:
            if evaluation_ancestor_limit is not None:
                raise ValueError("evaluation limit requires a frozen evaluation")
            head = substrate.rpc_request("chain_getFinalizedHead", [])["result"]
        carried = list(prior_responses or [])
        segmented = isinstance((history_checkpoint or {}).get("segments"), list)
        if segmented:
            response_sha = history_checkpoint.get("responses_sha256")
        else:
            response_sha = hashlib.sha256(_response_bytes(carried)).hexdigest() if history_checkpoint is not None else None
        checkpoint_error = None
        stop = ({"hash": "0x" + round_contract["block_hash"], "number": round_contract["block_number"]}
                if report_only else None)
        if history_checkpoint is not None:
            if segmented:
                checkpoint_error = segments_problem(history_checkpoint, None if history_dir is None else Path(history_dir))
            if checkpoint_error is None:
                checkpoint_error = checkpoint_problem(
                    history_checkpoint,
                    genesis_hash=round_contract["genesis_hash"],
                    responses_sha256=response_sha,
                    ark_coldkey=round_contract["ark_coldkey"],
                    ark_validator_hotkey=round_contract["ark_validator_hotkey"],
                    eligibility_policy_version=round_contract["eligibility_policy_version"],
                )
            if checkpoint_error is None:
                stop = {"hash": history_checkpoint["head_hash"], "number": history_checkpoint["through_block"]}
        if checkpoint_error is not None:
            # A supplied checkpoint that does not match is not a reason to scan from genesis.
            blocks, walk_errors = [], [checkpoint_error]
        else:
            blocks, walk_errors = _walk(substrate, head, stop=stop, max_blocks=10001 if report_only else None)
        if evaluation is not None and blocks:
            boundary = blocks[-1]
            if (boundary['hash'] != '0x' + evaluation['block_hash']
                    or boundary['number'] != evaluation['block_number']
                    or boundary['timestamp_ms'] != evaluation['timestamp_ms']):
                raise ValueError('decoded evaluation boundary differs from frozen storage proof')
        if history_checkpoint is not None and checkpoint_error is None and not prefix_landed(blocks, history_checkpoint):
            checkpoint_error = "history_fork"
        prefix = history_checkpoint if checkpoint_error is None and history_checkpoint is not None else None
        if report_only:
            actual_genesis = substrate.rpc_request("chain_getBlockHash", [0])["result"]
            span = report_window_span(blocks, round_contract, walk_errors, actual_genesis)
        else:
            span = coverage_span(blocks, round_contract, walk_errors, prefix=prefix)
        time_complete = span["time_complete"]
        observed_from = span["observed_from"]
        observed_through = span["observed_through"]
        genesis = span["genesis"]
        window = [
            row for row in blocks
            if isinstance(row["timestamp_ms"], int)
            and (report_only or round_contract["commit_open_ms"] <= row["timestamp_ms"] < round_contract["reveal_close_ms"])
        ]
        bare_genesis = genesis[2:] if isinstance(genesis, str) and genesis.startswith("0x") else genesis
        calls = []
        raw_blocks = {}
        decode_errors = []
        for row in window:
            try:
                decoded, raw = _decode_block(substrate, row, mining_netuid=mining_netuid_filter)
            except Exception as exc:
                decode_errors.append(f"block {row['number']}: {type(exc).__name__}")
                continue
            for call in decoded:
                call["genesis_hash"] = bare_genesis
            calls.extend(decoded)
            if not capture_window:
                raw_blocks[row["number"]] = raw
        if report_only:
            history_rows, history_errors, history_missing = [], [], []
        elif checkpoint_error is not None:
            history_rows, history_errors, history_missing = [], [checkpoint_error], []
        else:
            covered = prefix["through_block"] if prefix is not None else -1
            scanned_rows, history_errors, history_missing = _history(substrate, round_contract, blocks, covered_through=covered)
            earlier = [row for row in (prefix["receipts"] if prefix is not None else []) if row["block"] < round_contract["block_number"]]
            history_rows = earlier + scanned_rows
        history_errors = walk_errors + decode_errors + history_errors
        history_complete = time_complete and not history_missing and not history_errors
    else:
        if shared_window['binding'] != binding:
            raise ValueError('shared report window identity differs')
        raw_metadata = shared_window['raw_metadata']
        version = shared_window['version']
        metadata_sha = hashlib.sha256(bytes.fromhex(raw_metadata[2:])).hexdigest()
        evaluation_proof = shared_window['evaluation_proof']
        blocks = shared_window['blocks']
        walk_errors = shared_window['walk_errors']
        calls = shared_window['calls']
        decode_errors = shared_window['decode_errors']
        span = shared_window['span']
        time_complete = shared_window['time_complete']
        observed_from = shared_window['observed_from']
        observed_through = shared_window['observed_through']
        genesis = shared_window['genesis']
        history_rows = shared_window['history_rows']
        history_errors = shared_window['history_errors']
        history_missing = shared_window['history_missing']
        history_complete = shared_window['history_complete']
        raw_blocks, carried, prefix, checkpoint_error = {}, [], None, None
    if capture_window and shared_window is None:
        registrations = {hotkey: _registration(substrate, round_contract, _account_id(hotkey))
                         for hotkey in hotkeys}
    elif capture_window:
        registrations = shared_window['registrations']
        if set(registrations) != set(hotkeys):
            raise ValueError('shared snapshot roster differs')
    response_start = len(getattr(substrate, '_response_log', []))
    if capture_window and shared_window is None:
        shared_window = {'binding': binding, 'registrations': registrations,
                         'raw_metadata': raw_metadata,
                         'version': version,
                         'evaluation_proof': evaluation_proof,
                         'blocks': blocks,
                         'walk_errors': walk_errors,
                         'calls': calls,
                         'decode_errors': decode_errors,
                         'span': span,
                         'time_complete': time_complete,
                         'observed_from': observed_from,
                         'observed_through': observed_through,
                         'genesis': genesis,
                         'history_rows': history_rows,
                         'history_errors': history_errors,
                         'history_missing': history_missing,
                         'history_complete': history_complete,
                         'response_count': response_start}
    pin = "0x" + round_contract["block_hash"]
    if shared_engine is not None:
        shared_engine.begin_round()
        source = shared_engine
        calc_receipts = shared_engine.receipts
    elif chain_source is None:
        client = RpcClient(urls, pin, budget.transport if budget else websocket_transport, run_budget_s=180)
        source = LiveSource(client)
        calc_receipts = client.receipts
    else:
        source = chain_source
        calc_receipts = []
    engine, _meta = compute(
        source,
        round_contract["reported_netuid"],
        pin,
        round_contract["block_number"],
        ark=bytes.fromhex(round_contract["ark_coldkey"]),
    )
    calculation = calculation_for_shadow(round_contract, engine, allow_unavailable_outputs=True) if engine.get("status") == "OK" else None
    observation = _observation(substrate, hotkeys, round_contract["mining_netuid"], version, metadata_sha, endpoint)
    evidences = {}
    for hotkey in hotkeys:
        raw_key = _account_id(hotkey)
        selected = select_for_miner(
            calls,
            hotkey_hex=raw_key.hex(),
            mining_netuid=round_contract["mining_netuid"],
            commit_open_ms=round_contract["commit_open_ms"],
            commit_close_ms=round_contract["commit_close_ms"],
            reveal_open_ms=round_contract["reveal_open_ms"],
            reveal_close_ms=round_contract["reveal_close_ms"],
            **({"expected_round": round_contract} if report_only else {}),
        )
        registered, owner = (registrations[hotkey] if capture_window else
                             _registration(substrate, round_contract, raw_key))
        evidences[raw_key.hex()] = _one(
            round_contract,
            selected,
            registered,
            owner,
            raw_key.hex(),
            history_rows,
            history_complete,
            history_missing,
            history_errors,
            time_complete,
            observed_from,
            observed_through,
            calculation,
            genesis,
            blocks,
        )
        if report_only:
            evidences[raw_key.hex()]["deposit_history"] = {"state": "NOT_APPLICABLE"}
    directory = None
    if out_dir is not None:
        if endpoint in ('reader_a', 'reader_b'):
            for receipt in calc_receipts:
                receipt.endpoint = endpoint
        fresh = list(getattr(substrate, "_response_log", []))
        combined = fresh[response_start:] if capture_window else carried + fresh
        checkpoint = None
        if not report_only and history_complete and not history_missing and not history_errors:
            if prefix is not None and isinstance(prefix.get("segments"), list):
                # The prefix files stay where the bootstrap wrote them. Do not
                # hash the suffix as if it were the whole archive.
                if prefix["through_block"] >= round_contract["block_number"] - 1:
                    checkpoint = dict(prefix)
                    if history_dir is not None:
                        checkpoint.pop("segment_root", None)
            else:
                checkpoint = make_checkpoint(
                    round_contract,
                    blocks if prefix is None else blocks,
                    history_rows,
                    observed_from,
                    hashlib.sha256(_response_bytes(combined)).hexdigest(),
                )
        directory = _retain(
            out_dir,
            round_contract,
            evidences,
            observation,
            raw_metadata,
            raw_blocks,
            blocks,
            calc_receipts,
            clock,
            combined,
            hotkeys,
            checkpoint,
            history_dir=history_dir,
            engine_result=engine,
            evaluation_settings=None if evaluation is None else {
                "evaluation": evaluation, "max_ancestor_distance": evaluation_ancestor_limit},
            evaluation_proof=evaluation_proof,
            omit_window=capture_window,
        )
    return {"directory": None if directory is None else str(directory), "evidence": evidences,
            "observation": observation, "engine": engine, "evaluation_proof": evaluation_proof,
            **({"shared_window": shared_window} if capture_window else {})}


class _SubstrateSource:
    """Use the collector's recorded raw RPC session for cutoff reconstruction."""
    def __init__(self, substrate):
        self.substrate = substrate

    def call(self, method, params):
        response = self.substrate.rpc_request(method, params)
        if not isinstance(response, dict) or "error" in response or "result" not in response:
            raise ValueError("evaluation response unavailable")
        return response["result"]


def _one(round_contract, selected, registered, owner, hotkey, history_rows, history_complete, history_missing, history_errors, time_complete, observed_from, observed_through, calculation, genesis, blocks):
    number = round_contract["block_number"]
    block_b = next((row for row in blocks if row["number"] == number), None)
    commit = selected["commit"]
    reveal = selected["reveal"]
    return {
        "calculation": calculation or {
            "ark_coldkey": round_contract["ark_coldkey"],
            "block_hash": round_contract["block_hash"],
            "block_number": number,
            "calculation_version": round_contract["calculation_version"],
            "coverage": {"coverage_gap_exceeds_holder": True},
            "genesis_hash": round_contract["genesis_hash"],
            "netuid": round_contract["reported_netuid"],
            "output1": {"drop_ppm": 0, "status": "UNAVAILABLE"},
            "output2": {"alpha_required_rao": 0, "status": "UNAVAILABLE"},
            "output3": {"alpha_required_rao": 0, "status": "UNAVAILABLE"},
            "status": "UNAVAILABLE",
        },
        "candidates": {
            "commits": [commit] if commit else [],
            "diagnostics": selected["diagnostics"],
            "reveals": [reveal] if reveal else [],
        },
        "collection": {
            "errors": history_errors,
            "from_ms": observed_from,
            "gaps": [[item, item] for item in history_missing],
            "genesis_hash": genesis[2:] if genesis.startswith("0x") else genesis,
            "round_block_hash": block_b["hash"][2:] if block_b else "",
            "state": "COMPLETE" if time_complete else "INCOMPLETE",
            "through_ms": observed_through,
        },
        "collector": COLLECTOR_ID,
        "commit": commit,
        "deposit_history": {
            "eligibility_policy_version": round_contract["eligibility_policy_version"],
            "errors": history_errors,
            "from_block": 0 if history_complete else 1,
            "gaps": [[item, item] for item in history_missing],
            "genesis_hash": genesis[2:] if genesis.startswith("0x") else genesis,
            "ledger_kind": "native" if round_contract["reported_netuid"] == round_contract["mining_netuid"] else "foreign",
            "receipts": history_rows if history_complete else [],
            "round_block_hash": block_b["hash"][2:] if block_b else "",
            "state": "COMPLETE" if history_complete else "INCOMPLETE",
            "through_block": number - 1 if history_complete else max(number - 2, 0),
        },
        "finality": {
            "commit": None if commit is None else commit.get("finality"),
            "reveal": None if reveal is None else reveal.get("finality"),
        },
        "reveal": reveal,
        "snapshot": {
            "block_hash": block_b["hash"][2:] if block_b else "",
            "block_number": number,
            "coldkey": owner or ("00" * 32),
            "finalized": bool(block_b and block_b.get("finalized")),
            "genesis_hash": genesis[2:] if isinstance(genesis, str) and genesis.startswith("0x") else genesis,
            "hotkey": hotkey,
            "registered": registered,
            "timestamp_ms": block_b["timestamp_ms"] if block_b else 0,
        },
    }


def _attach_recorder(substrate, budget=None, *, immutable_cache=False):
    """Keep every raw response the collector asks the node for."""
    original = substrate.rpc_request
    rows = []
    from .rpc_cache import ImmutableRpcCache, immutable_key
    cache = (ImmutableRpcCache(max_bytes=budget.total_limit, max_entries=budget.max_calls)
             if immutable_cache and budget is not None else None)
    substrate._immutable_rpc_cache = cache

    def wrapped(method, params=None, result_handler=None):
        key = immutable_key(method, params, result_handler) if cache is not None else None
        if cache is not None:
            check_failure = getattr(getattr(substrate, 'websocket', None), 'check_failure', None)
            if callable(check_failure):
                check_failure()
            else:
                key = None  # An unknown transport cannot bypass its own failure checks.
            budget.remaining()
            cached = cache.get(key)
            if cached is not None:
                return cached
        response = original(method, params, result_handler)
        if result_handler is None and isinstance(response, dict) and "result" in response:
            # Copy now. Later decoders mutate the same object.
            encoded = json.dumps(response["result"])
            if budget:
                budget.retain(len(encoded.encode('utf-8')))
            result = json.loads(encoded)
            digest, size = result_sha256(result)
            stored = list(params) if isinstance(params, (list, tuple)) else []
            rows.append({
                "byte_length": size,
                "method": method,
                "params": stored,
                "result": result,
                "sha256": digest,
            })
            if cache is not None:
                cache.put(key, response)
        return response

    substrate.rpc_request = wrapped
    return rows


def _connect(urls, budget=None, *, immutable_cache=False):
    last = None
    for url in urls:
        substrate = None
        try:
            substrate = (SubstrateInterface(websocket=budget.socket(url), auto_discover=False)
                         if budget else live_sdk(url, auto_discover=False))
            substrate._response_log = _attach_recorder(substrate, budget=budget, immutable_cache=immutable_cache)
            substrate.init_runtime()
            return substrate, endpoint_label(url)
        except Exception as exc:
            if substrate is not None:
                try:
                    substrate.close()
                except Exception:
                    pass
            last = exc
    raise RuntimeError("no endpoint accepted a runtime connection") from last


def coverage_span(blocks, round_contract, walk_errors, *, prefix=None) -> dict:
    """Whether finalized timestamps actually cover the round.

    ``observed_through`` is the latest timestamp read from a block. It is never
    replaced with the requested ``reveal_close``. A walk that ends earlier is
    incomplete even when every available block was read. Block 0 has no
    timestamp extrinsic on this runtime.
    """
    numbers = sorted(row["number"] for row in blocks)
    stamps = [row["timestamp_ms"] for row in blocks if isinstance(row["timestamp_ms"], int)]
    observed_through = max(stamps) if stamps else 0
    if prefix is None:
        contiguous = numbers == list(range(min(numbers), max(numbers) + 1)) if numbers else False
        observed_from = min(stamps) if stamps else 0
        genesis = next((row["hash"] for row in blocks if row["number"] == 0), "")
        starts_at_zero = bool(numbers) and numbers[0] == 0
    else:
        anchor = prefix["through_block"]
        contiguous = numbers == list(range(anchor, numbers[-1] + 1)) if numbers else False
        observed_from = prefix["observed_from_ms"]
        genesis = "0x" + prefix["genesis_hash"]
        starts_at_zero = prefix.get("from_block") == 0 and prefix_landed(blocks, prefix)
    identity_ok = (
        isinstance(genesis, str)
        and genesis[2:] == round_contract["genesis_hash"]
        and any(
            row["number"] == round_contract["block_number"]
            and row["hash"][2:] == round_contract["block_hash"]
            and row["timestamp_ms"] == round_contract["block_timestamp_ms"]
            for row in blocks
        )
    )
    time_complete = (
        identity_ok
        and contiguous
        and numbers
        and starts_at_zero
        and observed_from <= round_contract["commit_open_ms"]
        and observed_through >= round_contract["reveal_close_ms"]
        and not walk_errors
        and all(isinstance(row["timestamp_ms"], int) for row in blocks if row["number"] != 0)
    )
    return {
        "genesis": genesis,
        "observed_from": observed_from,
        "observed_through": observed_through,
        "time_complete": time_complete,
    }


def _timestamp_ms(block):
    for extrinsic in block.get("extrinsics") or []:
        call = extrinsic.value.get("call") or {}
        if call.get("call_module") != "Timestamp" or call.get("call_function") != "set":
            continue
        args = call.get("call_args") or []
        if isinstance(args, list):
            args = {item["name"]: item["value"] for item in args}
        if "now" in args:
            return int(args["now"])
    return None


def _response_bytes(rows: list) -> bytes:
    """Canonical uncompressed log. The digest does not depend on gzip metadata."""
    if not rows:
        return b""
    return "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows).encode() + b"\n"


def _response_file_bytes(rows: list) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(_response_bytes(rows))
    return buffer.getvalue()


def report_window_span(blocks, round_contract, walk_errors, genesis):
    """Prove complete B-to-cutoff coverage, without claiming deposit history."""
    if not blocks:
        return {"genesis": genesis, "observed_from": 0, "observed_through": 0, "time_complete": False}
    ordered = sorted(blocks, key=lambda row: row["number"])
    first, last = ordered[0], ordered[-1]
    clocks = [row["timestamp_ms"] for row in ordered]
    complete = (
        genesis == "0x" + round_contract["genesis_hash"]
        and first["number"] == round_contract["block_number"]
        and first["hash"] == "0x" + round_contract["block_hash"]
        and first["timestamp_ms"] == round_contract["block_timestamp_ms"]
        and all(type(stamp) is int for stamp in clocks)
        and all(right > left for left, right in zip(clocks, clocks[1:]))
        and all(row["finalized"] is True for row in ordered)
        and all(right["number"] == left["number"] + 1 and right["parent"] == left["hash"]
                for left, right in zip(ordered, ordered[1:]))
        and first["timestamp_ms"] <= round_contract["commit_open_ms"]
        and last["timestamp_ms"] >= round_contract["reveal_close_ms"]
        and not walk_errors
    )
    return {"genesis": genesis, "observed_from": clocks[0] or 0,
            "observed_through": clocks[-1] or 0, "time_complete": bool(complete)}


def _walk(substrate, head, stop=None, *, max_blocks=None):
    rows = []
    errors = []
    cursor = head.lower()
    seen = set()
    while cursor and cursor not in seen:
        if max_blocks is not None and len(rows) >= max_blocks:
            raise ValueError("report collection exceeds block budget")
        seen.add(cursor)
        header = substrate.rpc_request("chain_getHeader", [cursor])["result"]
        number = int(header["number"], 16)
        if rows and number != rows[-1]["number"] - 1:
            raise ValueError("report collection ancestry discontinuity")
        if max_blocks is not None and not rows and stop is not None and number - stop["number"] >= max_blocks:
            raise ValueError("report collection exceeds block budget")
        try:
            timestamp = _timestamp_ms(substrate.get_block(block_hash=cursor))
        except Exception as exc:
            timestamp = None
            errors.append(f"block {number}: {type(exc).__name__}")
        if timestamp is None and number != 0 and not any(item.startswith(f"block {number}:") for item in errors):
            errors.append(f"block {number}: timestamp extrinsic missing")
        rows.append({
            "finalized": True,
            "finality": None,
            "hash": cursor,
            "header": header,
            "number": number,
            "parent": header["parentHash"].lower(),
            "timestamp_ms": timestamp,
        })
        if number == 0:
            break
        if stop is not None and (cursor == str(stop["hash"]).lower() or number <= stop["number"]):
            break
        cursor = header["parentHash"].lower()
    by_hash = {row["hash"]: row for row in rows}
    head_row = by_hash[head.lower()]

    def header_of(one_hash):
        row = by_hash[one_hash.lower()]
        return row["number"], row["parent"]

    for row in rows:
        row["finality"] = prove_ancestry(
            head_hash=head.lower(),
            head_number=head_row["number"],
            inclusion_hash=row["hash"],
            inclusion_number=row["number"],
            header_of=header_of,
        )
        row["finalized"] = row["finality"].get("matches") is True
    rows.sort(key=lambda row: row["number"])
    return rows, errors


def _body_has_commitment(block, mining_netuid=None):
    pending = [extrinsic.value for extrinsic in block["extrinsics"]]
    seen = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list, tuple)):
            continue
        identity = id(value)
        if identity in seen or len(seen) >= 100000:
            return True  # Do not infer absence from an opaque/cyclic structure.
        seen.add(identity)
        if isinstance(value, dict):
            if value.get("call_module") == "Commitments" and value.get("call_function") == "set_commitment":
                if mining_netuid is None:
                    return True
                args = value.get("call_args")
                if isinstance(args, list):
                    try:
                        args = {item["name"]: item["value"] for item in args}
                    except (KeyError, TypeError):
                        return True
                if not isinstance(args, dict) or type(args.get("netuid")) is not int or args["netuid"] == mining_netuid:
                    return True
            pending.extend(value.values())
        else:
            pending.extend(value)
    return False


def _decode_block(substrate, row, *, mining_netuid=None):
    raw = substrate.rpc_request("chain_getBlock", [row["hash"]])["result"]
    block = substrate.get_block(block_hash=row["hash"])
    # Event success is relevant only if a decoded body contains a commitment
    # call. Inspect nested calls too; unknown/cyclic structures conservatively
    # retain the old event read. Direct-call selection semantics stay unchanged.
    events = (substrate.query("System", "Events", block_hash=row["hash"]).value or []
              if _body_has_commitment(block, mining_netuid) else [])
    calls = []
    for index, extrinsic in enumerate(block["extrinsics"]):
        value = extrinsic.value
        call = value.get("call") or {}
        if call.get("call_module") != "Commitments" or call.get("call_function") != "set_commitment":
            continue
        args = call.get("call_args") or {}
        if isinstance(args, list):
            args = {item["name"]: item["value"] for item in args}
        if mining_netuid is not None and type(args.get("netuid")) is int and args["netuid"] != mining_netuid:
            continue
        payload = _payload(args.get("info"))
        if payload is None:
            continue
        names = {event.get("event_id") for event in events if event.get("extrinsic_idx") == index}
        signer = str(value.get("address") or "")
        if signer.startswith("0x"):
            signer = signer[2:]
        else:
            try:
                signer = ss58_decode(signer).hex()
            except Exception:
                signer = ""
        calls.append({
            "block_hash": row["hash"][2:],
            "block_number": row["number"],
            "call": "Commitments.set_commitment",
            "data_hex": payload.hex(),
            "extrinsic_index": index,
            "finality": row["finality"],
            "finalized": row["finalized"] is True,
            "genesis_hash": "",
            "signer": signer.lower(),
            "structurally_valid": _valid_commitment_payload(payload) and len(signer) == 64,
            "successful": "ExtrinsicSuccess" in names and "ExtrinsicFailed" not in names,
            "target_netuid": int(args.get("netuid")),
            "timestamp_ms": row["timestamp_ms"],
        })
    return calls, raw


def _valid_commitment_payload(payload):
    if len(payload) in {32, 203, 207}:
        return True
    from .protocol.context_commitment import decode_commitment
    try:
        decode_commitment(payload)
        return True
    except ValueError:
        return False


def _payload(info):
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"Raw32", "BigRaw"} and isinstance(item, str) and item.startswith("0x"):
                    found.append(bytes.fromhex(item[2:]))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(info)
    return found[0] if found else None


def _ark_self_stake_indexes(extrinsics, ark_ss58: str) -> set[int]:
    """Extrinsics where the Ark coldkey itself buys stake.

    That purchase is the operator's own position. It is not a miner deposit,
    and it has no giver event. ``deposits_in_block`` already ignores the same
    purchase when it is wrapped in ``Proxy.proxy``. A direct ``add_stake``
    signed by the Ark is the same fact, so it is left out of the event list
    before that reader runs. The real extrinsic is still passed through, so
    the proxy case keeps its own check.
    """
    ark = ss58_decode(ark_ss58)
    indexes = set()
    for index, value in enumerate(extrinsics):
        call = value.get("call") or {}
        if (call.get("call_module"), call.get("call_function")) != ("SubtensorModule", "add_stake"):
            continue
        address = value.get("address")
        if not isinstance(address, str):
            continue
        try:
            signer = bytes.fromhex(address[2:]) if address.startswith("0x") else ss58_decode(address)
        except Exception:
            continue
        if signer == ark:
            indexes.add(index)
    return indexes


def receipts_for_block(row, events, extrinsics, ark_ss58: str, validator_ss58: str) -> list:
    """Deposits in one block. The same reader the collector and the bootstrap use."""
    own_stake = _ark_self_stake_indexes(extrinsics, ark_ss58)
    records = []
    for index, event in enumerate(events):
        if event.get("extrinsic_idx") in own_stake:
            continue
        records.append({
            "attributes": event.get("attributes"),
            "event_id": event.get("event_id"),
            "extrinsic_idx": event.get("extrinsic_idx"),
            "module_id": event.get("module_id"),
            "ordinal_in_block": index,
            "phase": event.get("phase"),
        })
    found = deposits_in_block(
        records,
        ark_ss58,
        row["number"],
        row["hash"],
        validator_ss58,
        extrinsics=extrinsics,
        parent_block_hash=row["parent"],
        owner_source=lambda *_args, **_kwargs: None,
    )
    rows = []
    for deposit in found:
        rows.append({
            "amount_rao": deposit.amount_rao,
            "block": deposit.block,
            "coldkey": ss58_decode(deposit.owner_at_deposit).hex() if deposit.owner_at_deposit else "00" * 32,
            "destination_coldkey": ss58_decode(ark_ss58).hex(),
            "destination_hotkey": ss58_decode(deposit.hotkey).hex() if deposit.hotkey else "00" * 32,
            "event_index": deposit.event_ordinal,
            "origin_hotkey": ss58_decode(deposit.origin_hotkey).hex() if deposit.origin_hotkey else "00" * 32,
            "owner_at_deposit": ss58_decode(deposit.owner_at_deposit).hex() if deposit.owner_at_deposit else "00" * 32,
            "token_netuid": deposit.token_netuid,
            "unattributed_reason": deposit.unattributed_reason,
        })
    return rows


def _history(substrate, round_contract, blocks, *, covered_through=-1):
    rows = []
    errors = []
    present = {row["number"] for row in blocks}
    missing = [number for number in range(covered_through + 1, round_contract["block_number"]) if number not in present]
    ark = ss58_encode(bytes.fromhex(round_contract["ark_coldkey"]))
    validator = ss58_encode(bytes.fromhex(round_contract["ark_validator_hotkey"]))
    for row in blocks:
        if row["number"] <= covered_through or row["number"] >= round_contract["block_number"]:
            continue
        try:
            events = substrate.query("System", "Events", block_hash=row["hash"]).value or []
            decoded = substrate.get_block(block_hash=row["hash"])
            extrinsics = [item.value if isinstance(item.value, dict) else {} for item in decoded.get("extrinsics") or []]
            found = receipts_for_block(row, events, extrinsics, ark, validator)
        except (Exception, Refusal, NotADepositEvent) as exc:
            errors.append(f"block {row['number']}: {type(exc).__name__}")
            continue
        rows.extend(found)
    return rows, errors, missing


def _registration(substrate, round_contract, hotkey: bytes):
    ss58 = ss58_encode(hotkey)
    pin = "0x" + round_contract["block_hash"]
    uid = substrate.query("SubtensorModule", "Uids", [round_contract["mining_netuid"], ss58], block_hash=pin).value
    owner = substrate.query("SubtensorModule", "Owner", [ss58], block_hash=pin).value
    try:
        owner_hex = ss58_decode(owner).hex() if isinstance(owner, str) else ""
    except Exception:
        owner_hex = ""
    return uid is not None and len(owner_hex) == 64, owner_hex


def _account_id(value):
    if isinstance(value, str) and re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}', value):
        return bytes.fromhex(value.removeprefix('0x'))
    return ss58_decode(value)


def _observation(substrate, hotkeys, mining_netuid, version, metadata_sha, endpoint):
    uids = substrate.get_metadata_storage_function("SubtensorModule", "Uids")
    owner = substrate.get_metadata_storage_function("SubtensorModule", "Owner")
    sample = hotkeys[0] if hotkeys else None
    created = None
    identity = None
    if sample:
        raw = _account_id(sample)
        created = substrate.create_storage_key("SubtensorModule", "Uids", [mining_netuid, ss58_encode(raw)]).to_hex()
        identity = hex_encode(uids_key(mining_netuid, raw))
    return {
        "endpoint": endpoint,
        "genesis": str(substrate.get_block_hash(0)).lower(),
        "metadata_sha256": metadata_sha,
        "owner_hashers": owner.value["type"]["Map"]["hashers"],
        "spec_name": version.get("specName"),
        "spec_version": version.get("specVersion"),
        "uids_hashers": uids.value["type"]["Map"]["hashers"],
        "uids_key_matches_identity_blake2": created == identity,
    }


def _retain(out_dir, round_contract, evidences, observation, raw_metadata, raw_blocks, blocks, receipts, clock, responses, hotkeys, checkpoint, *, history_dir=None, engine_result=None, evaluation_settings=None, evaluation_proof=None, omit_window=False):
    body = json.dumps({"evidence": evidences, "observation": observation}, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(body).hexdigest()
    directory = Path(out_dir) / round_contract["round_date"] / (digest + "-" + secrets.token_hex(4))
    os.makedirs(directory, exist_ok=False)
    if not omit_window:
        _exclusive(directory / "evidence.json", body)
    if not omit_window:
        _exclusive(directory / "metadata.hex", raw_metadata.encode())
    if engine_result is not None:
        from noah.reports.artifacts import canonical_bytes, engine_binding
        _exclusive(directory / "engine.json", canonical_bytes(engine_result))
        _exclusive(directory / "engine-binding.json", canonical_bytes(engine_binding(round_contract, engine_result)))
    if evaluation_settings is not None:
        from noah.reports.artifacts import canonical_bytes
        _exclusive(directory / "evaluation.json", canonical_bytes(evaluation_settings))
        _exclusive(directory / "evaluation-proof.json", canonical_bytes(evaluation_proof))
    if not omit_window:
        _exclusive(directory / "coverage.json", json.dumps([
            {"hash": row["hash"], "number": row["number"], "timestamp_ms": row["timestamp_ms"]} for row in blocks
        ]).encode())
    if not omit_window:
        for number, raw in raw_blocks.items():
            _exclusive(directory / f"block-{number}.json", json.dumps(raw, sort_keys=True).encode())
    save_receipts(str(directory / "receipts.jsonl.gz"), receipts)
    _exclusive(directory / "hotkeys.json", json.dumps(list(hotkeys), sort_keys=True).encode())
    _exclusive(directory / "responses.jsonl.gz", _response_file_bytes(responses))
    if checkpoint is not None and isinstance(checkpoint.get("segments"), list):
        problem = segments_problem(checkpoint, Path(history_dir))
        if problem:
            raise ValueError(problem)
        for segment in checkpoint["segments"]:
            relative = segment["file"]
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(history_dir) / relative, target)
            os.chmod(target, 0o600)
    if checkpoint is not None:
        _exclusive(directory / "history-checkpoint.json", json.dumps(checkpoint, sort_keys=True).encode())
    _exclusive(directory / "collection.json", json.dumps({
        "collected_at_unix": int(clock()),
        "endpoint": observation["endpoint"],
        "sha256": digest,
        "through_ms_observed": max((row["timestamp_ms"] for row in blocks if isinstance(row["timestamp_ms"], int)), default=0),
    }, sort_keys=True).encode())
    _exclusive(directory / "round.json", json.dumps(round_contract, sort_keys=True).encode())
    return directory


def _exclusive(path: Path, payload: bytes) -> None:
    from .prepare import _publish_exclusive
    _publish_exclusive(path, payload)
