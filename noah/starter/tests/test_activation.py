import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.chain.store import Store
from noah.core.settings import SettingsError
from noah.research.tests.test_entry_migration import FLOORED_ANCHOR, REG, snapshot
from noah.starter import migrate
from noah.starter.runtime import configure


def test_migration_cli_uses_readonly_complete_store_and_refuses_overwrite(tmp_path):
    path = tmp_path / "history.db"
    store = Store.create(path, day_anchor_ts=FLOORED_ANCHOR)
    store.mark_day_scanned(0, 1, 100)
    store.mark_day_scanned(1, 101, 101)
    store.close()
    facts = dict(
        dayAnchorTimestamp=FLOORED_ANCHOR,
        subnetRegistrationDay=0,
        asOfDay=1,
        registrationDays=REG,
        genesisHash="test-genesis",
        netuid=82,
        finalizedBlock=101,
        finalizedHash="test-finalized",
    )
    fact_path = tmp_path / "facts.json"
    fact_path.write_text(json.dumps(facts))
    out = tmp_path / "migration.json"
    before = path.read_bytes()
    args = ["migrate", "--history-db", str(path), "--facts", str(fact_path), "--out", str(out)]
    with patch("sys.argv", args):
        migrate.main()
        with pytest.raises(FileExistsError):
            migrate.main()
    assert path.read_bytes() == before
    assert json.loads(out.read_text())["payload"]["legacyEntryRao"] == 10000000000


def test_opt_in_requires_features_profile_and_version_floor(tmp_path):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=FLOORED_ANCHOR)
    store.mark_day_scanned(0, 1, 100)
    store.mark_day_scanned(1, 101, 101)
    saved = snapshot(store)
    store.close()
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(saved))
    settings = SimpleNamespace(
        starter_entry_snapshot=path,
        epoch_collection=True,
        epoch_republication=True,
        publish_mode="direct",
    )
    a = SimpleNamespace(
        _publish_intent=object(),
        _submitted_record_dir=tmp_path,
        runner_chain=SimpleNamespace(
            head_block=lambda: 200, starter_epoch_info=lambda h: {"WeightsVersionKey": 0}
        ),
        publisher=SimpleNamespace(version_key=0),
    )
    with patch(
        "noah.scoring.parameters.load_parameters",
        return_value=SimpleNamespace(profile_version="mainnet-production-starter-25bps-v1"),
    ):
        configure(a, settings)
        assert a.publisher.version_key > 0
        a.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 2**64 - 1}
        with pytest.raises(SettingsError, match="chain floor"):
            configure(a, settings)
    with pytest.raises(SettingsError, match="separately versioned"):
        configure(a, settings)
    settings.epoch_republication = False
    with pytest.raises(SettingsError, match="both epoch"):
        configure(a, settings)
