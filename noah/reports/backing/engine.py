"""Slice-1 calculation. Chain inputs come from the source; this module adds price, drop, H, and search."""

from __future__ import annotations

import time
from typing import Any

from .constants import (
    ARK_PUBKEY_HEX,
    COVERAGE_LEGACY, COVERAGE_STAKED_V2,
    MECHANISM_DYNAMIC,
    MINIMUM_RESERVE,
    RAO_STEP,
    SUBNET_STATE_DISSOLVING,
    SUBNET_STATE_PENDING_DISSOLUTION,
    THRESHOLD_10,
    THRESHOLD_20,
)
from .keys import hex_decode, ss58_encode
from .reads import Reader
from .result import alpha_decimal, ark_context, definitions, percent_string
from .rpc import RunFailure
from .scale import SimSwap
from .search import SaleProbeError, ThresholdSearch, boundary_drop, invalid_sale_reason
from .source import ReplayError
from .u64f64 import calculate_price, drop_ppm, price_1e9

_ARK = hex_decode(ARK_PUBKEY_HEX)


class _Timer:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.phases: dict[str, int] = {}

    def run(self, name: str, fn):
        started = time.perf_counter()
        try:
            return fn()
        finally:
            self.phases[name] = self.phases.get(name, 0) + int((time.perf_counter() - started) * 1000)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


def compute(source, netuid, block_hash, block_number=None, *, ark=None):
    """Fresh calculation always requires the current holder-coverage evidence."""
    result, meta = _compute(source, netuid, block_hash, block_number, ark=ark,
                            coverage_method=COVERAGE_STAKED_V2)
    result["coverage_method"] = COVERAGE_STAKED_V2
    return result, meta


def replay_compute(source, netuid, block_hash, block_number=None, *, ark=None, coverage_method):
    """Private retained-input path; never a network or fresh-production option."""
    from .source import ReplaySource
    if not isinstance(source, ReplaySource):
        raise ValueError("coverage replay requires retained raw inputs")
    if coverage_method == COVERAGE_STAKED_V2:
        return compute(source, netuid, block_hash, block_number, ark=ark)
    if coverage_method != COVERAGE_LEGACY:
        raise ValueError("unknown retained coverage method")
    return _compute(source, netuid, block_hash, block_number, ark=ark)


def _compute(
    source,
    netuid: int,
    block_hash: str,
    block_number: int | None = None,
    *,
    ark: bytes | None = None,
    coverage_method: str = COVERAGE_LEGACY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Calculate one subnet at one block.

    `ark` selects which coldkey is excluded from H and reported as Ark context.
    The default is the production Ark. Passing another key does not change the
    price, drop, or threshold arithmetic.
    """
    if coverage_method not in (COVERAGE_LEGACY, COVERAGE_STAKED_V2):
        raise ValueError("unsupported coverage method")
    block_hash = block_hash.lower()
    ark_key = _ARK if ark is None else ark
    timer = _Timer()
    reader = Reader(source, block_hash)
    meta: dict[str, Any] = {"netuid": netuid, "block_hash": block_hash, "phases_ms": {}}
    try:
        number, timestamp_ms = timer.run("block_check", lambda: reader.ensure_block(block_number))
        spec = timer.run("block_check", reader.spec_version)
        meta["block_number"] = number
        meta["spec_version"] = spec
        if not reader.qualified_runtime(spec):
            return _gated("UNSUPPORTED", "runtime_spec_not_pinned", netuid, block_hash, number, spec), _finish(meta, timer, reader, source)
        pool = timer.run("pool_reads", lambda: _read_pool(reader, netuid))
        if isinstance(pool, tuple):
            status, reason = pool
            return _gated(status, reason, netuid, block_hash, number, spec), _finish(meta, timer, reader, source)
        if coverage_method == COVERAGE_STAKED_V2:
            reader.require_staked_coverage()
            pool["stake_pool_total"] = reader.total_alpha_staked(netuid)
        coldkeys = timer.run("alpha_v2_scan", lambda: reader.discover_coldkeys(netuid))
        rows = timer.run("availability", lambda: reader.availability(coldkeys, netuid))
        _consistency(rows)
        if (coverage_method == COVERAGE_STAKED_V2
                and sum(row.total for row in rows.values()) > pool["stake_pool_total"]):
            raise RunFailure("stake_coverage_observed_exceeds_pool")
        timer.run("lock_cross_check", lambda: _cross_check(reader, netuid, rows))
        holder, tie_count = _select_holder(rows, ark_key)
        if holder is None:
            return _gated("UNAVAILABLE", "no_holder", netuid, block_hash, number, spec), _finish(meta, timer, reader, source)
        pubkey, row = holder
        output1 = timer.run(
            "output1_sim",
            lambda: _output1(reader, netuid, pool, row.available),
        )
        output2 = timer.run(
            "threshold_10",
            lambda: _threshold(reader, netuid, pool, THRESHOLD_10, 0.10),
        )
        output3 = timer.run(
            "threshold_20",
            lambda: _threshold(reader, netuid, pool, THRESHOLD_20, 0.20),
        )
        result = _success(
            netuid, block_hash, number, timestamp_ms, spec, pool, rows, pubkey, row, tie_count, output1, output2, output3, ark_key
        )
        meta["threshold_sim_calls"] = {
            "10": output2.get("sim_calls"),
            "20": output3.get("sim_calls"),
        }
        return result, _finish(meta, timer, reader, source)
    except ReplayError:
        raise
    except (RunFailure, SaleProbeError) as exc:
        gated = _gated( "UNAVAILABLE", exc.reason, netuid, block_hash, meta.get("block_number"), meta.get("spec_version"))
        return gated, _finish(meta, timer, reader, source)


def _read_pool(reader: Reader, netuid: int):
    state = reader.subnet_state(netuid)
    if state is None:
        return ("UNAVAILABLE", "subnet_not_found")
    if state in (SUBNET_STATE_PENDING_DISSOLUTION, SUBNET_STATE_DISSOLVING):
        return ("UNAVAILABLE", "subnet_dissolving")
    if reader.mechanism(netuid) != MECHANISM_DYNAMIC:
        return ("UNSUPPORTED", "stable_mechanism")
    if not reader.subtoken_enabled(netuid):
        return ("UNAVAILABLE", "subtoken_disabled")
    if not reader.pool_initialized(netuid):
        return ("UNAVAILABLE", "pool_uninitialized")
    alpha_in = reader.alpha_in(netuid)
    tao = reader.tao(netuid)
    if alpha_in < MINIMUM_RESERVE or tao < MINIMUM_RESERVE:
        return ("UNAVAILABLE", "reserves_too_low")
    quote = reader.quote_parts(netuid)
    price = calculate_price(alpha_in, tao, quote)
    if price == 0:
        return ("UNAVAILABLE", "zero_price")
    return {
        "alpha_in": alpha_in,
        "tao": tao,
        "alpha_out": reader.alpha_out(netuid),
        "quote_parts": quote,
        "fee_rate": reader.fee_rate(netuid),
        "price_bits": price,
        "price_1e9": price_1e9(price),
    }


def _consistency(rows: dict) -> None:
    for row in rows.values():
        if row.total - row.locked - row.available < 0:
            raise RunFailure("availability_inconsistent")


def _cross_check(reader: Reader, netuid: int, rows: dict) -> None:
    for coldkey, row in rows.items():
        if row.available >= row.total:
            continue
        locked = reader.lock_mass(coldkey, netuid)
        locked_mass = 0 if locked is None else locked
        collateral = reader.collateral(netuid, coldkey)
        if locked_mass != row.locked:
            raise RunFailure("lock_cross_check_failed")
        remainder = row.total - row.locked
        derived = 0 if remainder < collateral else remainder - collateral
        if derived != row.available:
            raise RunFailure("lock_cross_check_failed")


def _select_holder(rows: dict, ark: bytes):
    candidates = [(pubkey, row) for pubkey, row in rows.items() if pubkey != ark]
    if not candidates:
        return None, 0
    best = max(row.available for _pubkey, row in candidates)
    tied = [(pubkey, row) for pubkey, row in candidates if row.available == best]
    tied.sort(key=lambda item: item[0])
    return tied[0], len(tied)


def _output1(reader: Reader, netuid: int, pool: dict, amount: int) -> dict[str, Any]:
    if amount > 1000 * pool["alpha_in"]:
        return {"status": "UNAVAILABLE", "reason": "above_input_cap"}
    sim = reader.sim(netuid, amount)
    reason = invalid_sale_reason(sim, amount, pool["alpha_in"], pool["tao"])
    if reason is not None:
        return {"status": "UNAVAILABLE", "reason": reason}
    return {"status": "OK", "sim": sim, "amount": amount}


def _threshold(reader: Reader, netuid: int, pool: dict, ratio: tuple[int, int], drop: float) -> dict[str, Any]:
    def probe(amount: int) -> SimSwap:
        try:
            return reader.sim(netuid, amount)
        except RunFailure as exc:
            raise SaleProbeError(exc.reason) from exc

    search = ThresholdSearch(
        x=pool["alpha_in"],
        y=pool["tao"],
        quote_parts=pool["quote_parts"],
        fee_rate=pool["fee_rate"],
        price_before=pool["price_bits"],
        probe=probe,
    )
    outcome = search.search(ratio[0], ratio[1], drop)
    payload: dict[str, Any] = {
        "status": outcome.status,
        "reason": outcome.reason,
        "sim_calls": outcome.sim_calls,
        "k": outcome.k,
        "observations": outcome.observations,
    }
    return payload


def _success(netuid, block_hash, number, timestamp_ms, spec, pool, rows, pubkey, row, tie_count, output1, output2, output3, ark: bytes):
    sum_total = sum(item.total for item in rows.values())
    sum_locked = sum(item.locked for item in rows.values())
    sum_available = sum(item.available for item in rows.values())
    sum_available_ex_ark = sum(item.available for key, item in rows.items() if key != ark)
    alpha_out = pool["alpha_out"]
    coverage_total = pool.get("stake_pool_total", alpha_out)
    gap = 0 if sum_total >= coverage_total else coverage_total - sum_total
    coverage_ppm = (sum_total * 1_000_000) // coverage_total if coverage_total else 0
    ark_row = rows.get(ark)
    return {
        "ark_context": ark_context(
            ark_row is not None,
            0 if ark_row is None else ark_row.total,
            0 if ark_row is None else ark_row.locked,
            0 if ark_row is None else ark_row.available,
        ),
        "block_hash": block_hash,
        "block_number": number,
        "block_timestamp_ms": timestamp_ms,
        "coverage": {
            **({"basis": "total-alpha-staked-v2", "stake_pool_total_rao": pool["stake_pool_total"]}
               if "stake_pool_total" in pool else {}),
            "coldkey_count": len(rows),
            "coverage_gap_exceeds_holder": gap > row.available,
            "coverage_ppm": coverage_ppm,
            "gap_rao": gap,
            "subnet_alpha_out_rao": alpha_out,
            "sum_available_rao": sum_available,
            "sum_locked_rao": sum_locked,
            "sum_total_rao": sum_total,
        },
        "definitions": definitions(),
        "flags": _flags(output2, output3, sum_available, sum_available_ex_ark),
        "holder": {
            "available_rao": row.available,
            "collateral_derived_rao": row.total - row.locked - row.available,
            "locked_rao": row.locked,
            "pubkey": "0x" + pubkey.hex(),
            "ss58": ss58_encode(pubkey),
            "tie_count": tie_count,
            "total_rao": row.total,
        },
        "netuid": netuid,
        "output1": _format_output1(pool, output1),
        "output2": _format_threshold(pool, output2, 10),
        "output3": _format_threshold(pool, output3, 20),
        "pool": {
            "alpha_in_rao": pool["alpha_in"],
            "alpha_out_rao": pool["alpha_out"],
            "fee_rate": pool["fee_rate"],
            "price_1e9": pool["price_1e9"],
            "price_bits": pool["price_bits"],
            "quote_parts": pool["quote_parts"],
            "tao_rao": pool["tao"],
        },
        "ark_coldkey": "0x" + ark.hex(),
        "reason": None,
        "schema": definitions()["schema"],
        "spec_version": spec,
        "status": "OK",
    }


def _flags(output2, output3, sum_available, sum_available_ex_ark) -> dict[str, Any]:
    flags = {
        "sum_available_all_rao": sum_available,
        "sum_available_ex_ark_rao": sum_available_ex_ark,
    }
    for name, output in (("10", output2), ("20", output3)):
        amount = None if output.get("k") is None else output["k"] * RAO_STEP
        flags[f"exceeds_available_all_{name}"] = bool(amount is not None and amount > sum_available)
        flags[f"exceeds_available_ex_ark_{name}"] = bool(amount is not None and amount > sum_available_ex_ark)
    return flags


def _format_output1(pool: dict, output: dict) -> dict[str, Any]:
    if output["status"] != "OK":
        return {"reason": output["reason"], "status": "UNAVAILABLE"}
    sim: SimSwap = output["sim"]
    price_after = calculate_price(pool["alpha_in"] + sim.alpha_amount, pool["tao"] - sim.tao_amount, pool["quote_parts"])
    ppm = drop_ppm(pool["price_bits"], price_after)
    return {
        "alpha_amount_rao": sim.alpha_amount,
        "alpha_fee_rao": sim.alpha_fee,
        "alpha_sold_rao": output["amount"],
        "drop_ppm": ppm,
        "percent": percent_string(ppm),
        "price_after_bits": price_after,
        "reason": None,
        "status": "OK",
        "tao_amount_rao": sim.tao_amount,
    }


def _format_threshold(pool: dict, output: dict, percent: int) -> dict[str, Any]:
    if output["status"] != "OK":
        return {"percent_target": percent, "reason": output["reason"], "sim_calls": output["sim_calls"], "status": "UNAVAILABLE"}
    k = output["k"]
    observation = output["observations"][k]
    sim = observation.sim
    ppm = boundary_drop(observation, pool["price_bits"])
    numerator, denominator = (9, 10) if percent == 10 else (4, 5)
    previous = output["observations"].get(k - 1)
    nxt = output["observations"].get(k + 1)
    earlier = output["observations"].get(k - 2)
    return {
        "alpha_decimal": alpha_decimal(k * RAO_STEP),
        "alpha_fee_rao": sim.alpha_fee,
        "alpha_required_rao": k * RAO_STEP,
        "drop_ppm": ppm,
        "k": k,
        "k_minus_1": _boundary(previous, pool["price_bits"], numerator, denominator),
        "k_minus_2": _boundary(earlier, pool["price_bits"], numerator, denominator),
        "k_plus_1": _boundary(nxt, pool["price_bits"], numerator, denominator),
        "percent_target": percent,
        "price_after_bits": observation.price_bits,
        "reason": None,
        "sim_calls": output["sim_calls"],
        "status": "OK",
        "tao_amount_rao": sim.tao_amount,
    }


def _boundary(observation, price_before: int, numerator: int, denominator: int) -> dict[str, Any] | None:
    if observation is None:
        return None
    if not observation.valid:
        return {"k": observation.k, "reason": observation.reason, "valid": False}
    return {
        "drop_ppm": boundary_drop(observation, price_before),
        "k": observation.k,
        "price_after_bits": observation.price_bits,
        "qualifies": observation.qualifies(price_before, numerator, denominator),
        "valid": True,
    }


def _gated(status: str, reason: str, netuid: int, block_hash: str, number, spec) -> dict[str, Any]:
    return {
        "block_hash": block_hash,
        "block_number": number,
        "definitions": definitions(),
        "netuid": netuid,
        "reason": reason,
        "schema": definitions()["schema"],
        "spec_version": spec,
        "status": status,
    }


def _finish(meta: dict, timer: _Timer, reader: Reader, source) -> dict[str, Any]:
    meta["phases_ms"] = timer.phases
    meta["total_ms"] = timer.total_ms()
    meta["discovery_pages"] = reader.page_count
    meta["positions_on_netuid"] = reader.position_count
    client = getattr(source, "client", None)
    if client is not None:
        meta["retries"] = dict(client.retries)
        meta["connect_ms"] = client.connect_ms
        meta["endpoints"] = sorted({receipt.endpoint for receipt in client.receipts})
    return meta
