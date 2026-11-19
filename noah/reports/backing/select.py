"""Per-miner commitment selection. Decoded calls in, no chain reads.

For one hotkey, the commitment is the earliest successful, structurally valid
``Commitments.set_commitment`` that targets the mining subnet and falls inside
the commit window. Order is block number, then extrinsic index. A later
commitment does not replace it.

The reveal is the earliest successful, structurally valid
``Commitments.set_commitment`` from that signer that targets the mining
subnet, falls inside the reveal window, and whose bytes hash to that
commitment. An earlier match on another subnet does not replace it. Other
attempts are kept for diagnostics and are not used to hunt for a payload
that would verify.

When the report-only collector supplies ``expected_round``, an absent timely
choice can fall back to an observed out-of-window pair. Its revealed preimage
must bind this exact round before a bare commitment is attributed to it. This
only explains EARLY/LATE outcomes; it never replaces a timely selected call.
The caller supplies only blocks from B through its frozen evaluation boundary.
"""

from __future__ import annotations

import hashlib


def select_for_miner(calls: list[dict], *, hotkey_hex: str, mining_netuid: int, commit_open_ms: int, commit_close_ms: int, reveal_open_ms: int, reveal_close_ms: int, expected_round: dict | None = None) -> dict:
    from .protocol.context_commitment import enabled
    if expected_round is not None and enabled(expected_round):
        return _select_context(calls, hotkey_hex, mining_netuid, expected_round,
                               commit_open_ms, commit_close_ms, reveal_open_ms, reveal_close_ms)
    hotkey = hotkey_hex.lower()
    own = [call for call in calls if str(call.get("signer", "")).lower() == hotkey]
    others = [call for call in calls if str(call.get("signer", "")).lower() != hotkey]
    commitments = [
        call for call in own
        if _commitment(call, mining_netuid) and commit_open_ms <= int(call["timestamp_ms"]) < commit_close_ms
    ]
    commitments.sort(key=lambda call: (int(call["block_number"]), int(call["extrinsic_index"])))
    chosen = next((call for call in commitments if call.get("successful") is True and call.get("structurally_valid") is True), None)
    attempts = [
        call for call in own
        if reveal_open_ms <= int(call["timestamp_ms"]) < reveal_close_ms
        and isinstance(call.get("data_hex"), str)
        and len(call["data_hex"]) in (406, 414)
    ]
    attempts.sort(key=lambda call: (int(call["block_number"]), int(call["extrinsic_index"])))
    matching = []
    if chosen is not None:
        digest = bytes.fromhex(chosen["data_hex"])
        matching = [call for call in attempts if _reveal(call, mining_netuid, hotkey) and _matches(call, digest)]
    selected = matching[0] if matching else None
    if expected_round is not None:
        chosen, selected = _diagnostic_pair(
            own, chosen, selected, expected_round, hotkey, mining_netuid,
            commit_open_ms, commit_close_ms, reveal_open_ms, reveal_close_ms,
        )
    return {
        "commit": chosen,
        "diagnostics": {
            "later_commitments": [call for call in commitments if call is not chosen],
            "other_miners": len(others),
            "reveals": [call for call in attempts if call is not selected],
        },
        "reveal": selected,
    }


def _order(call):
    return int(call["block_number"]), int(call["extrinsic_index"])


def _diagnostic_pair(own, chosen, selected, contract, hotkey, netuid,
                     commit_open, commit_close, reveal_open, reveal_close):
    from .protocol.preimage import REPORT_POLICY
    if selected is not None or contract.get("eligibility_policy_version") != REPORT_POLICY:
        return chosen, selected
    # A mismatched optional context must not enlarge the accepted windows or
    # give an unrelated round's missing report a confident diagnostic.
    if (contract.get("mining_netuid") != netuid or
            tuple(contract.get(k) for k in ("commit_open_ms", "commit_close_ms",
                "reveal_open_ms", "reveal_close_ms")) !=
            (commit_open, commit_close, reveal_open, reveal_close)):
        raise ValueError("report selection context differs")
    reveals = sorted((call for call in own
        if _reveal(call, netuid, hotkey) and _current_round(call, contract, hotkey)), key=_order)
    if chosen is not None:
        # Never move from the earliest timely commitment to a later commitment
        # just because that later pair would succeed.
        digest = bytes.fromhex(chosen["data_hex"])
        selected = next((call for call in reveals if _matches(call, digest)
            and not reveal_open <= int(call["timestamp_ms"]) < reveal_close), None)
        return chosen, selected
    commitments = sorted((call for call in own
        if _commitment(call, netuid) and call.get("successful") is True
        and call.get("structurally_valid") is True
        and _observed_context(call, contract)
        and not commit_open <= int(call["timestamp_ms"]) < commit_close), key=_order)
    for candidate in commitments:
        digest = bytes.fromhex(candidate["data_hex"])
        matches = [call for call in reveals if _matches(call, digest)]
        if matches:
            # Retain deterministic earliest timely reveal if available, then
            # earliest observed diagnostic reveal. Neither can make this
            # out-of-window commitment pass verification.
            reveal = next((call for call in matches
                if reveal_open <= int(call["timestamp_ms"]) < reveal_close), matches[0])
            return candidate, reveal
    return None, None


def _current_round(call, contract, hotkey):
    from .protocol.preimage import ReportSubmission, decode
    try:
        payload = decode(bytes.fromhex(call["data_hex"]))
        return (isinstance(payload, ReportSubmission)
            and payload.genesis_hash.hex() == contract["genesis_hash"]
            and payload.hotkey.hex() == hotkey
            and payload.block_number == contract["block_number"]
            and payload.block_hash.hex() == contract["block_hash"]
            and payload.netuid == contract["reported_netuid"]
            and payload.mining_netuid == contract["mining_netuid"]
            and payload.release_prefix.hex() == contract["release_prefix"]
            and payload.round_yyyymmdd == int(contract["round_date"].replace("-", ""))
            and _observed_context(call, contract))
    except (KeyError, TypeError, ValueError):
        return False


def _observed_context(call, contract):
    return (call.get("genesis_hash") == contract["genesis_hash"]
        and int(call["block_number"]) >= contract["block_number"]
        and int(call["timestamp_ms"]) >= contract["block_timestamp_ms"])


def _commitment(call: dict, mining_netuid: int) -> bool:
    return (
        call.get("call") == "Commitments.set_commitment"
        and call.get("target_netuid") == mining_netuid
        and isinstance(call.get("data_hex"), str)
        and len(call["data_hex"]) == 64
    )


def _reveal(call: dict, mining_netuid: int, hotkey: str) -> bool:
    return (
        call.get("call") == "Commitments.set_commitment"
        and call.get("target_netuid") == mining_netuid
        and str(call.get("signer", "")).lower() == hotkey
        and call.get("successful") is True
        and call.get("structurally_valid") is True
        and isinstance(call.get("data_hex"), str)
        and len(call["data_hex"]) in (406, 414)
    )


def _matches(call: dict, digest: bytes) -> bool:
    raw = call.get("data_hex")
    if not isinstance(raw, str) or len(raw) not in (406, 414):
        return False
    return hashlib.sha256(bytes.fromhex(raw)).digest() == digest


def _select_context(calls, hotkey_hex, netuid, contract, commit_open, commit_close, reveal_open, reveal_close):
    """The first valid context envelope stands, independently of all other rounds."""
    from .protocol.context_commitment import matches_context, matches_reveal
    # The caller cannot use context for one round with another round's windows.
    if (netuid != contract['mining_netuid'] or
            tuple(contract[k] for k in ('commit_open_ms', 'commit_close_ms', 'reveal_open_ms', 'reveal_close_ms'))
            != (commit_open, commit_close, reveal_open, reveal_close)):
        raise ValueError('report selection context differs')
    hotkey = hotkey_hex.lower()
    own = [call for call in calls if str(call.get('signer', '')).lower() == hotkey]
    def valid_call(call):
        return (call.get('call') == 'Commitments.set_commitment' and call.get('target_netuid') == netuid
                and call.get('successful') is True and call.get('structurally_valid') is True
                and call.get('finalized') is True and _observed_context(call, contract))
    def contextual(call):
        try:
            return valid_call(call) and matches_context(bytes.fromhex(call['data_hex']), contract)
        except (KeyError, TypeError, ValueError):
            return False
    commits = sorted((c for c in own if contextual(c)), key=_order)
    timely = [c for c in commits if commit_open <= int(c['timestamp_ms']) < commit_close]
    # An out-of-window envelope is diagnostic only; it never displaces a timely one.
    chosen = next(iter(timely), None)
    if chosen is None:
        chosen = next(iter(commits), None)
    attempts = sorted((c for c in own if valid_call(c) and _reveal(c, netuid, hotkey)), key=_order)
    matches = []
    if chosen is not None:
        raw = bytes.fromhex(chosen['data_hex'])
        for call in attempts:
            try:
                if matches_reveal(raw, bytes.fromhex(call['data_hex'])):
                    matches.append(call)
            except (TypeError, ValueError):
                continue
    selected = next((c for c in matches if reveal_open <= int(c['timestamp_ms']) < reveal_close), None)
    if selected is None:
        selected = next(iter(matches), None)
    return {'commit': chosen, 'reveal': selected, 'diagnostics': {
        'later_commitments': [c for c in commits if c is not chosen],
        'other_miners': len(calls) - len(own), 'reveals': [c for c in attempts if c is not selected]}}
