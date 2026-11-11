"""Complete cutoff evidence by coldkey/hotkey, independently of active UIDs.

Capture is an explicit read-only preparation operation, never a normal legacy
scoring requirement. Shared wallet collateral is not a financial identity.
"""

import hashlib
from dataclasses import asdict

from ..core.calculation_record import encoded
from ..scoring.engine import score_miner
from ..scoring.fixture_io import parse_fixture
from .engine import legacy_shape, replay_state

SCHEMA = "noah-points-lineages/1"
POLICY_SCHEMA = "noah-points-policy/2"


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pair(value):
    return value["coldkey"], value["hotkey"]


def lineage_state(template, row):
    raw = {**template, "miners": [row]}
    fixture = parse_fixture(raw)
    old = score_miner(fixture.miners[0], fixture.dials, fixture.eval_day)
    points = replay_state(parse_fixture(legacy_shape(raw)).miners[0], int(template["evalDay"]))
    return dict(
        state={**asdict(points), "balance": old.coverage_reserve_rao - old.debt_rao},
        legacy_coverage_rao=old.coverage_reserve_rao,
        legacy_debt_rao=old.debt_rao,
    )


def capture(
    adapter,
    store,
    fixture_store,
    fixture,
    seats,
    hotkeys,
    foreign_rule,
    entry_deposits,
    registration_days,
    cutoff_block,
):
    """Capture all attributed historical pairs using the existing accounting readers.

    The caller has completed dual-reader ingestion through this cutoff. Inactive
    lineages carry uid:null: they are accounting inputs, never fabricated seats.
    """
    from ..chain.fixture_build import (
        MissingFromStore,
        _first_activity_day,
        _lock_hold_out,
        _rows_for_coldkey,
    )
    from ..chain.history_check import require_whole_history

    cutoff = int(fixture["evalDay"])
    require_whole_history(store, through_day=cutoff)
    if store.scanned_range(cutoff)[1] != cutoff_block:
        raise ValueError("lineage capture must use the completed cutoff day")
    inventory = store.financial_lineage_inventory(cutoff)
    active = {
        (row["coldkey"], hotkeys[row["uid"]]): row
        for row in fixture["miners"]
        if row["uid"] is not None
    }
    from ..chain.entry_history import selected
    snapshot = selected(adapter).snapshot
    original = {pair(row): row for row in snapshot["payload"]["records"]}
    pairs = set(active) | {pair(row) for row in inventory["pairs"]} | set(original)
    rows = []
    valuations = []

    def record_foreign(amount, netuid, block, block_hash):
        value = foreign_rule(amount, netuid, block, block_hash)
        valuations.append(
            dict(
                block=block,
                block_hash=block_hash,
                token_netuid=netuid,
                amount_rao=str(amount),
                noah_equivalent_rao=str(value),
            )
        )
        return value

    for cold, hot in sorted(pairs):
        if (cold, hot) in active:
            miner = {**active[cold, hot], "uid": None, "holdRao": "0"}
        else:
            first = _first_activity_day(
                fixture_store, cold, cutoff, entry_deposits, registration_days
            )
            if first is None:
                # A foreign-only historical receipt did not create legacy credit;
                # its actual day still belongs to the frozen evidence population.
                first = next(
                    (r["first_day"] for r in inventory["pairs"] if pair(r) == (cold, hot)), None
                )
            if first is None:
                # A frozen entry identity with no financial history still has a
                # genuine registration day, not an invented financial event.
                first = original[cold, hot]["registrationDay"]
            reason = _lock_hold_out(store, cold, first, cutoff)
            if reason:
                raise MissingFromStore("lineage capture has unresolved collateral evidence")
            # Do not invent historical chain registration facts. With no live
            # registration and no credited entry, the ordinary accounting row
            # uses its documented first-activity default, as a legacy null row
            # does; this is never passed to EntryPolicy as chain evidence.
            per_registration = (
                {cold: {hot: registration_days[cold][hot]}}
                if hot in registration_days.get(cold, {})
                else {}
            )
            built = _rows_for_coldkey(
                fixture_store,
                cold,
                first,
                cutoff,
                {cold: {hot: None}},
                record_foreign,
                entry_deposits,
                per_registration,
                lineage_only=True,
            )
            if len(built) != 1:
                raise ValueError("lineage capture did not produce exactly one identity")
            miner = built[0]
        rows.append(dict(coldkey=cold, hotkey=hot, input=miner))
    result = dict(
        schema=SCHEMA,
        genesis=adapter.runner_chain.genesis_hash(),
        netuid=adapter.netuid,
        cutoff_day=cutoff,
        cutoff_block=cutoff_block,
        cutoff_hash=adapter.runner_chain.block_hash(cutoff_block),
        entry_snapshot_sha256=snapshot["sha256"],
        entry_pairs=[dict(coldkey=cold, hotkey=hot) for cold, hot in sorted(original)],
        inventory=inventory,
        records=rows,
        foreign_valuations=valuations,
    )
    result["source_sha256"] = digest(result)
    return result


def complete(capture, fixture, migration, *, netuid):
    """Validate capture completeness and derive exact per-lineage cutoff state."""
    if not isinstance(capture, dict) or set(capture) != {
        "schema",
        "genesis",
        "netuid",
        "cutoff_day",
        "cutoff_block",
        "cutoff_hash",
        "entry_snapshot_sha256",
        "entry_pairs",
        "inventory",
        "records",
        "foreign_valuations",
        "source_sha256",
    }:
        raise ValueError("complete historical lineage capture is required")
    if (
        any(
            type(capture[k]) is not int or capture[k] < 0
            for k in ("netuid", "cutoff_day", "cutoff_block")
        )
        or capture["schema"] != SCHEMA
        or capture["netuid"] != netuid
        or any(
            capture[k] != migration[k]
            for k in ("genesis", "cutoff_day", "cutoff_block", "cutoff_hash")
        )
    ):
        raise ValueError("lineage cutoff identity differs")
    if capture["source_sha256"] != digest(
        {k: v for k, v in capture.items() if k != "source_sha256"}
    ):
        raise ValueError("lineage source digest differs")
    import re

    if not re.fullmatch("[0-9a-f]{64}", capture["entry_snapshot_sha256"]):
        raise ValueError("lineage entry classification pin missing")
    inventory = capture["inventory"]
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"pairs", "counts", "events_sha256"}
        or not re.fullmatch("[0-9a-f]{64}", inventory["events_sha256"])
        or set(inventory["counts"]) != {"earnings", "native", "foreign"}
        or any(type(v) is not int or v < 0 for v in inventory["counts"].values())
    ):
        raise ValueError("invalid lineage source inventory")
    financial = set()
    for row in inventory["pairs"]:
        if (
            set(row) not in (
                {"coldkey", "hotkey", "first_day", "block", "block_hash"},
                {"coldkey", "hotkey", "first_day", "block", "block_hash", "receipt"},
            )
            or type(row["first_day"]) is not int
            or not 0 <= row["first_day"] <= migration["cutoff_day"]
            or type(row["block"]) is not int
            or not 0 < row["block"] <= migration["cutoff_block"]
            or not isinstance(row["block_hash"], str)
            or not re.fullmatch("0x[0-9a-f]{64}", row["block_hash"])
            or pair(row) in financial
        ):
            raise ValueError("invalid or duplicate financial lineage")
        if "receipt" in row:
            from .lineage_receipt import validate as validate_receipt

            validate_receipt(row, migration)
        financial.add(pair(row))
    entries = set()
    for row in capture["entry_pairs"]:
        if set(row) != {"coldkey", "hotkey"} or pair(row) in entries:
            raise ValueError("invalid entry lineage")
        entries.add(pair(row))
    active = {pair(row): row for row in migration["miners"]}
    expected = financial | entries | set(active)
    records = {}
    fixture_rows = {row["uid"]: row for row in fixture["miners"] if row["uid"] is not None}
    for row in capture["records"]:
        if set(row) != {"coldkey", "hotkey", "input"} or pair(row) in records:
            raise ValueError("invalid or duplicate lineage record")
        key = pair(row)
        if any(
            not isinstance(v, str) or not re.fullmatch("[1-9A-HJ-NP-Za-km-z]{47,49}", v)
            for v in key
        ):
            raise ValueError("invalid lineage identity")
        raw = row["input"]
        if raw["uid"] is not None or raw["coldkey"] != row["coldkey"] or raw["holdRao"] != "0":
            raise ValueError("historical lineage cannot claim an active UID or holding")
        state = lineage_state(fixture, raw)
        if key in active:
            original = {**fixture_rows[active[key]["uid"]], "uid": None, "holdRao": "0"}
            if raw != original or any(state[k] != active[key][k] for k in state):
                raise ValueError("active lineage differs from retained legacy calculation")
        records[key] = {**row, **state}
    if set(records) != expected:
        raise ValueError("historical lineage capture is incomplete or contains extra identities")
    valuations = capture["foreign_valuations"]
    if not isinstance(valuations, list):
        raise ValueError("invalid dormant foreign valuation evidence")
    total = 0
    for value in valuations:
        if (
            set(value)
            != {"block", "block_hash", "token_netuid", "amount_rao", "noah_equivalent_rao"}
            or type(value["block"]) is not int
            or not 0 < value["block"] <= migration["cutoff_block"]
            or not isinstance(value["block_hash"], str)
            or not re.fullmatch("0x[0-9a-f]{64}", value["block_hash"])
            or type(value["token_netuid"]) is not int
            or value["token_netuid"] < 0
            or any(
                not isinstance(value[k], str) or not re.fullmatch("[0-9]+", value[k])
                for k in ("amount_rao", "noah_equivalent_rao")
            )
        ):
            raise ValueError("invalid dormant foreign valuation evidence")
        total += int(value["noah_equivalent_rao"])
    if total != sum(
        int(day["foreignDepositNoahEquivRao"])
        for key, row in records.items()
        if key not in active
        for day in row["input"]["days"].values()
    ):
        raise ValueError("dormant foreign valuations do not reconcile")
    return records


def validate_document(document, *, netuid):
    """Pure recovery validation; runtime separately verifies chain and entry pins."""
    try:
        from .amendment import baseline
        document = baseline(document)
        if document.get("schema") != POLICY_SCHEMA:
            raise ValueError("points activation requires complete lineage policy schema /2")
        records = complete(
            document["lineages"], document["fixture"], document["migration"], netuid=netuid
        )
        for row in document["lineages"]["inventory"]["pairs"]:
            if (
                "receipt" in row
                and row["receipt"]["destination_hotkey"] != document["validator_hotkey"]
            ):
                raise ValueError("lineage receipt destination differs from the policy validator")
        return records
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed historical lineage policy") from exc
