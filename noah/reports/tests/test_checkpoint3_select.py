"""Selection on decoded calls. These are not runtime bytes and not a live chain."""

import pytest

from noah.reports.backing.select import select_for_miner
from noah.reports.backing.submit import SubmitError, _windows

HOT_A = "11" * 32
HOT_B = "22" * 32


def _call(hotkey, number, index, payload_hex, timestamp, netuid=82, success=True):
    return {
        "block_number": number,
        "call": "Commitments.set_commitment",
        "data_hex": payload_hex,
        "extrinsic_index": index,
        "signer": hotkey,
        "structurally_valid": True,
        "successful": success,
        "target_netuid": netuid,
        "timestamp_ms": timestamp,
    }


def test_direct_ark_add_stake_is_not_a_miner_deposit_index():
    import sys
    from pathlib import Path

    from substrateinterface import Keypair

    noah = str(Path(__file__).resolve().parents[2] / "services" / "noah")
    sys.path.insert(0, noah)
    try:
        from noah.reports.backing.collector import _ark_self_stake_indexes
    finally:
        if noah in sys.path:
            sys.path.remove(noah)
    ark = Keypair.generate_mnemonic()
    pair = Keypair.create_from_mnemonic(ark)
    other = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    indexes = _ark_self_stake_indexes([
        {"address": pair.ss58_address, "call": {"call_function": "add_stake", "call_module": "SubtensorModule"}},
        {"address": other.ss58_address, "call": {"call_function": "add_stake", "call_module": "SubtensorModule"}},
        {"address": pair.ss58_address, "call": {"call_function": "transfer_stake_and_hotkey", "call_module": "SubtensorModule"}},
    ], pair.ss58_address)
    assert indexes == {0}
    del ark


def test_earliest_commitment_is_kept_and_other_miner_is_ignored():
    first = "aa" * 32
    later = "bb" * 32
    reveal = "cc" * 207
    import hashlib
    # The reveal must hash to the first commitment.
    digest = hashlib.sha256(bytes.fromhex(reveal)).hexdigest()
    first = digest
    calls = [
        _call(HOT_B, 1, 0, "dd" * 32, 1_000),
        _call(HOT_A, 2, 1, first, 1_500),
        _call(HOT_A, 3, 0, later, 1_600),
        _call(HOT_A, 9, 0, reveal, 5_000),
        _call(HOT_A, 10, 1, "ab" * 207, 5_100),
        _call(HOT_A, 4, 0, "ee" * 32, 1_700, netuid=7),
    ]
    chosen = select_for_miner(
        calls,
        hotkey_hex=HOT_A,
        mining_netuid=82,
        commit_open_ms=1_000,
        commit_close_ms=2_000,
        reveal_open_ms=4_000,
        reveal_close_ms=8_000,
    )
    assert chosen["commit"]["data_hex"] == first
    assert chosen["commit"]["block_number"] == 2
    assert chosen["reveal"]["data_hex"] == reveal
    assert chosen["diagnostics"]["later_commitments"][0]["data_hex"] == later
    assert chosen["diagnostics"]["reveals"][0]["data_hex"] == "ab" * 207
    assert chosen["diagnostics"]["other_miners"] == 1


def test_failed_earlier_commitment_is_skipped():
    good = "ab" * 32
    calls = [
        _call(HOT_A, 1, 0, "ff" * 32, 1_100, success=False),
        _call(HOT_A, 2, 0, good, 1_200),
    ]
    chosen = select_for_miner(
        calls,
        hotkey_hex=HOT_A,
        mining_netuid=82,
        commit_open_ms=1_000,
        commit_close_ms=2_000,
        reveal_open_ms=4_000,
        reveal_close_ms=8_000,
    )
    assert chosen["commit"]["data_hex"] == good
    assert chosen["reveal"] is None


def test_two_hour_windows_stay_unlabeled_and_short_windows_must_say_accelerated():
    announcement = {"announced_at_ms": 0, "block_timestamp_ms": 0}
    windows = _windows(announcement, {"accelerated": False, "commit_window_ms": 7_200_000, "reveal_window_ms": 7_200_000})
    assert windows.reveal_close_ms == 14_400_000
    with pytest.raises(SubmitError, match="labeled accelerated"):
        _windows(announcement, {"commit_window_ms": 60_000, "reveal_window_ms": 60_000})


# Controlled block step. These tests do not wait and do not read a node.
BLOCK_MS = 12_000
TWO_HOURS_MS = 7_200_000


def _product_windows():
    from datetime import datetime, timezone

    from noah.reports.backing.protocol.timing import classify_commit, classify_reveal

    opened = int(datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp() * 1000)
    windows = _windows(
        {"announced_at_ms": opened, "block_timestamp_ms": opened},
        {"accelerated": False, "commit_window_ms": TWO_HOURS_MS, "reveal_window_ms": TWO_HOURS_MS},
    )
    assert windows.commit_close_ms - windows.commit_open_ms == TWO_HOURS_MS
    assert windows.reveal_close_ms - windows.reveal_open_ms == TWO_HOURS_MS
    assert windows.reveal_open_ms == windows.commit_close_ms
    return windows, classify_commit, classify_reveal


def test_product_two_hour_edges_and_adjacent_blocks_are_controlled_timestamps():
    """Opening, closing, the exact millisecond, and one block on either side.

    This is not the real-time rehearsal. The timestamps are supplied by the test.
    """
    import hashlib

    windows, classify_commit, classify_reveal = _product_windows()
    reveal = "cc" * 207
    digest = hashlib.sha256(bytes.fromhex(reveal)).hexdigest()
    commit_edges = (
        (windows.commit_open_ms - BLOCK_MS, "EARLY_COMMIT", False),
        (windows.commit_open_ms - 1, "EARLY_COMMIT", False),
        (windows.commit_open_ms, "COMMIT_OK", True),
        (windows.commit_open_ms + BLOCK_MS, "COMMIT_OK", True),
        (windows.commit_close_ms - BLOCK_MS, "COMMIT_OK", True),
        (windows.commit_close_ms - 1, "COMMIT_OK", True),
        (windows.commit_close_ms, "LATE_COMMIT", False),
        (windows.commit_close_ms + BLOCK_MS, "LATE_COMMIT", False),
    )
    for timestamp, label, inside in commit_edges:
        assert classify_commit(timestamp, windows) == label
        chosen = select_for_miner(
            [_call(HOT_A, 2, 0, digest, timestamp)],
            hotkey_hex=HOT_A,
            mining_netuid=82,
            commit_open_ms=windows.commit_open_ms,
            commit_close_ms=windows.commit_close_ms,
            reveal_open_ms=windows.reveal_open_ms,
            reveal_close_ms=windows.reveal_close_ms,
        )
        assert (chosen["commit"] is not None) is inside

    reveal_edges = (
        (windows.reveal_open_ms - BLOCK_MS, "EARLY_REVEAL", False),
        (windows.reveal_open_ms - 1, "EARLY_REVEAL", False),
        (windows.reveal_open_ms, "REVEAL_OK", True),
        (windows.reveal_open_ms + BLOCK_MS, "REVEAL_OK", True),
        (windows.reveal_close_ms - BLOCK_MS, "REVEAL_OK", True),
        (windows.reveal_close_ms - 1, "REVEAL_OK", True),
        (windows.reveal_close_ms, "LATE_REVEAL", False),
        (windows.reveal_close_ms + BLOCK_MS, "LATE_REVEAL", False),
    )
    commit_at = windows.commit_open_ms
    for timestamp, label, inside in reveal_edges:
        assert classify_reveal(timestamp, windows, committed_ok=True) == label
        chosen = select_for_miner(
            [
                _call(HOT_A, 2, 0, digest, commit_at),
                _call(HOT_A, 3, 0, reveal, timestamp),
            ],
            hotkey_hex=HOT_A,
            mining_netuid=82,
            commit_open_ms=windows.commit_open_ms,
            commit_close_ms=windows.commit_close_ms,
            reveal_open_ms=windows.reveal_open_ms,
            reveal_close_ms=windows.reveal_close_ms,
        )
        assert (chosen["reveal"] is not None) is inside
    # The shared instant closes the commit window and opens the reveal window.
    assert classify_commit(windows.commit_close_ms, windows) == "LATE_COMMIT"
    assert classify_reveal(windows.reveal_open_ms, windows, committed_ok=True) == "REVEAL_OK"


def test_coverage_that_stops_before_reveal_close_is_incomplete():
    """Every supplied block can be read and the scan is still incomplete.

    The recorded end is the last observed timestamp, not the requested deadline.
    """
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    windows, _, _ = _product_windows()
    noah = str(Path(__file__).resolve().parents[2] / "services" / "noah")
    sys.path.insert(0, noah)
    try:
        from noah.reports.backing.collector import coverage_span
        from noah.reports.backing.protocol.policy import ENGINE_PIN, release_prefix
        from noah.reports.verification import verify_collected
    finally:
        if noah in sys.path:
            sys.path.remove(noah)
    opened = int(datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp() * 1000)
    genesis = "ab" * 32
    block_hash = "cd" * 32
    round_contract = {
        "ark_coldkey": "11" * 32,
        "ark_validator_hotkey": "22" * 32,
        "block_hash": block_hash,
        "block_number": 1,
        "block_timestamp_ms": opened,
        "calculation_version": ENGINE_PIN,
        "commit_close_ms": windows.commit_close_ms,
        "commit_open_ms": windows.commit_open_ms,
        "eligibility_policy_version": "attributed-deposit-before-b-v1",
        "genesis_hash": genesis,
        "mining_netuid": 82,
        "release_prefix": release_prefix("synthetic-report-test").hex(),
        "reported_netuid": 1,
        "reveal_close_ms": windows.reveal_close_ms,
        "reveal_open_ms": windows.reveal_open_ms,
        "round_date": "2026-11-14",
        "schema": "noah.backing.round.v1",
        "scoring_release": "fixture-release-not-deployed",
    }
    early_stamp = windows.reveal_close_ms - BLOCK_MS

    def blocks(last_stamp):
        return [
            {"hash": "0x" + genesis, "number": 0, "timestamp_ms": None},
            {"hash": "0x" + block_hash, "number": 1, "timestamp_ms": opened},
            {"hash": "0x" + ("ee" * 32), "number": 2, "timestamp_ms": last_stamp},
        ]

    short = coverage_span(blocks(early_stamp), round_contract, [])
    assert short["time_complete"] is False
    assert short["observed_through"] == early_stamp
    assert short["observed_through"] != round_contract["reveal_close_ms"]
    exact_miss = coverage_span(blocks(windows.reveal_close_ms - 1), round_contract, [])
    assert exact_miss["time_complete"] is False
    assert exact_miss["observed_through"] == windows.reveal_close_ms - 1
    reached = coverage_span(blocks(windows.reveal_close_ms), round_contract, [])
    assert reached["time_complete"] is True
    assert reached["observed_through"] == windows.reveal_close_ms

    evidence = {
        "candidates": {"commits": [], "diagnostics": {}, "reveals": []},
        "collection": {
            "errors": [],
            "from_ms": windows.commit_open_ms,
            "gaps": [],
            "genesis_hash": genesis,
            "round_block_hash": block_hash,
            "state": "COMPLETE",
            "through_ms": early_stamp,
        },
        "collector": "noah.reports.backing.collector",
        "commit": None,
        "finality": {},
        "reveal": None,
        "snapshot": {
            "block_hash": block_hash,
            "block_number": 1,
            "coldkey": "33" * 32,
            "finalized": True,
            "genesis_hash": genesis,
            "hotkey": "44" * 32,
            "registered": True,
            "timestamp_ms": opened,
        },
    }
    early = verify_collected(round_contract, evidence)
    assert early["status"] == "UNVERIFIABLE"
    assert early["reason"] == "submission_collection_incomplete"
    evidence["collection"] = dict(evidence["collection"], through_ms=windows.reveal_close_ms)
    on_time = verify_collected(round_contract, evidence)
    assert on_time["status"] == "UNVERIFIABLE"
    assert on_time["reason"] == "calculation_context"
    from noah.reports.backing.round_config import calculation_for_shadow
    from noah.reports.tests.test_history import _engine
    evidence["calculation"] = calculation_for_shadow(round_contract, _engine(round_contract))
    on_time = verify_collected(round_contract, evidence)
    assert on_time["status"] == "MISSING"
    assert on_time["reason"] == "commit"
