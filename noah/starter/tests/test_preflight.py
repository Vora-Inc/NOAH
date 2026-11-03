import sqlite3
from types import SimpleNamespace

import pytest

from noah.chain.store import Store
from noah.starter.allocation import digest
from noah.starter.entry import EntryPolicyRefused
from noah.starter.preflight import ReadOnlyHistory, inspect


def fixture(tmp_path):
    anchor = 86400
    path = tmp_path / "history.db"
    store = Store.create(path, day_anchor_ts=anchor)
    store.mark_day_scanned(0, 1, 100)
    store.mark_day_scanned(1, 101, 101)
    store.close()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    history = ReadOnlyHistory(Store(conn, anchor), anchor)
    chain = SimpleNamespace(
        head_block=lambda: 102,
        genesis_hash=lambda: "review-genesis",
        block_timestamp=lambda b: anchor + (0 if b == 1 else 172799 if b == 101 else 172800),
        starter_epoch_info=lambda b: {
            "NetworkRegisteredAt": 1,
            "consensus": "Yuma",
            "WeightsVersionKey": 0,
        },
        seat_hotkeys=lambda b: {0: "review-hotkey"},
        hotkey_owners=lambda keys, b: {"review-hotkey": "review-coldkey"},
        registration_blocks=lambda b: {0: 1},
        block_hash=lambda b: f"hash-{b}",
    )
    settings = SimpleNamespace(
        network="local", local_genesis="review-genesis", netuid=2, entry_deposit_rao=10000000000
    )
    return path, conn, history, chain, settings


def test_preflight_freezes_and_verifies_without_writing_history(tmp_path):
    path, conn, history, chain, settings = fixture(tmp_path)
    before = path.read_bytes()
    try:
        frozen, result = inspect(chain, history, settings)
        assert result["cutoff_day"] == 1
        assert frozen["payload"]["records"][0]["registrationDay"] == 0
        assert inspect(chain, history, settings, frozen)[0] == frozen
    finally:
        conn.close()
    assert path.read_bytes() == before


def test_correct_digest_cannot_forge_registration_day(tmp_path):
    path, conn, history, chain, settings = fixture(tmp_path)
    try:
        frozen, _ = inspect(chain, history, settings)
        frozen["payload"]["records"][0]["registrationDay"] = 1
        frozen["sha256"] = digest(frozen["payload"])
        with pytest.raises(EntryPolicyRefused, match="registration days"):
            inspect(chain, history, settings, frozen)
    finally:
        conn.close()
