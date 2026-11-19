"""Adapt one ``noah.backing.round.v1`` object into miner preparation inputs.

``validate_round`` in the validator checker is the acceptance check. This module does
not reimplement it and does not read a chain. A fixture built here is synthetic.
``fixture_only: true`` is required by his current checker and is not live
integration.
"""

from __future__ import annotations

from .keys import ss58_decode, ss58_encode


def require_round(round_contract: dict) -> dict:
    """Accept the round only when ``validate_round`` accepts it."""
    from noah.reports.verification import validate_round

    if not isinstance(round_contract, dict):
        raise ValueError("round is not an object")
    validate_round(round_contract)
    return round_contract


def account_hex_to_ss58(account_hex: str) -> str:
    """Raw AccountId hex, with or without ``0x``, to the SS58 string prepare requires."""
    raw = _account_bytes(account_hex)
    return ss58_encode(raw)


def ss58_to_account_hex(address: str) -> str:
    """SS58 address to lowercase AccountId hex without ``0x``."""
    return ss58_decode(address).hex()


def round_yyyymmdd(round_date: str) -> int:
    """``YYYY-MM-DD`` to the payload's ``YYYYMMDD`` integer."""
    if not isinstance(round_date, str) or len(round_date) != 10 or round_date[4] != "-" or round_date[7] != "-":
        raise ValueError("round date must be YYYY-MM-DD")
    return int(round_date.replace("-", ""))


def preparation_from_round(
    round_contract: dict,
    *,
    hotkey_hex: str,
    coldkey_hex: str,
    deposit_block: int | None = None,
    deposit_event: int | None = None,
    endpoint: str,
) -> dict:
    """Keyword arguments for ``prepare``, excluding ``out``, ``compute_fn``, and ``salt``.

    The release prefix bytes are the round's 16-byte prefix. ``mining_netuid``
    is the round's non-root mining subnet, also bound to the miner configuration
    and the actual commitment call target by the product boundaries.
    """
    require_round(round_contract)
    prefix = bytes.fromhex(round_contract["release_prefix"])
    if len(prefix) != 16:
        raise ValueError("release prefix must be 16 bytes")
    return {
        "announcement": {
            "block_hash": _prefixed_hash(round_contract["block_hash"]),
            "block_number": round_contract["block_number"],
            "block_timestamp_ms": round_contract["block_timestamp_ms"],
            "genesis": _prefixed_hash(round_contract["genesis_hash"]),
            "netuid": round_contract["reported_netuid"],
            "round": round_yyyymmdd(round_contract["round_date"]),
        },
        "coldkey": account_hex_to_ss58(coldkey_hex),
        "deposit_block": deposit_block,
        "deposit_event": deposit_event,
        "endpoint": endpoint,
        "eligibility_policy": round_contract["eligibility_policy_version"],
        **({"context_round": round_contract} if "commitment_format" in round_contract else {}),
        "hotkey": account_hex_to_ss58(hotkey_hex),
        "mining_netuid": round_contract["mining_netuid"],
        "release_prefix_bytes": prefix,
        "reported_netuid": round_contract["reported_netuid"],
    }


def calculation_for_shadow(round_contract: dict, engine: dict, *, allow_unavailable_outputs=False) -> dict | None:
    """Copy integer outputs only after the engine result is for this round.

    The engine's own block hash, block number, reported subnet, and Ark
    coldkey have to match the round. This function does not replace those
    fields with the round's values when they differ.
    """
    require_round(round_contract)
    if not isinstance(engine, dict) or engine.get("status") != "OK":
        raise ValueError("calculation is not ready")
    if _bare_hash(engine.get("block_hash")) != round_contract["block_hash"]:
        raise ValueError("calculation block hash does not match the round")
    if engine.get("block_number") != round_contract["block_number"]:
        raise ValueError("calculation block number does not match the round")
    if engine.get("netuid") != round_contract["reported_netuid"]:
        raise ValueError("calculation subnet does not match the round")
    if _bare_hash(engine.get("ark_coldkey")) != round_contract["ark_coldkey"]:
        raise ValueError("calculation Ark does not match the round")
    coverage = engine.get("coverage")
    if not isinstance(coverage, dict) or type(coverage.get("coverage_gap_exceeds_holder")) is not bool:
        raise ValueError("calculation coverage is missing")
    if allow_unavailable_outputs and any(
        isinstance(engine.get(name), dict) and engine[name].get("status") == "UNAVAILABLE"
        for name in ("output1", "output2", "output3")
    ):
        # Preserve the full engine artifact without inventing an integer report.
        # Preparation keeps the strict default and cannot submit this result.
        return None
    return {
        "ark_coldkey": round_contract["ark_coldkey"],
        "block_hash": round_contract["block_hash"],
        "block_number": round_contract["block_number"],
        "calculation_version": round_contract["calculation_version"],
        "coverage": {"coverage_gap_exceeds_holder": coverage["coverage_gap_exceeds_holder"]},
        "genesis_hash": round_contract["genesis_hash"],
        "netuid": round_contract["reported_netuid"],
        "output1": _output(engine, "output1", "drop_ppm"),
        "output2": _output(engine, "output2", "alpha_required_rao"),
        "output3": _output(engine, "output3", "alpha_required_rao"),
        "status": "OK",
    }




def _output(engine: dict, name: str, field: str) -> dict:
    output = engine.get(name)
    if not isinstance(output, dict) or output.get("status") != "OK":
        raise ValueError(f"{name} is not ready")
    value = output.get(field)
    if type(value) is not int:
        raise ValueError(f"{name}.{field} is not an integer")
    return {"status": "OK", field: value}


def _account_hex(value: str) -> str:
    return _account_bytes(value).hex()


def _account_bytes(value: str) -> bytes:
    text = value[2:] if isinstance(value, str) and value.startswith(("0x", "0X")) else value
    if not isinstance(text, str):
        raise ValueError("account id is not hex")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError("account id is not hex") from exc
    if len(raw) != 32:
        raise ValueError("account id must be 32 bytes")
    return raw


def _prefixed_hash(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("hash must be 32 bytes without 0x")
    return "0x" + value


def _bare_hash(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.lower()
    if text.startswith("0x"):
        text = text[2:]
    return text
