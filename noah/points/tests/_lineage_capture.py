"""Synthetic capture envelope for retained active-fixture unit tests.

Real completeness is tested through Store/capture; this helper has no production
caller and makes no assertion about inactive identities in a historical dataset.
"""

import copy


def for_fixture(fixture, migration, *, netuid=82):
    by_uid = {row["uid"]: row for row in fixture["miners"] if row["uid"] is not None}
    rows = sorted(migration["miners"], key=lambda row: (row["coldkey"], row["hotkey"]))
    result = dict(
        schema="noah-points-lineages/1",
        genesis=migration["genesis"],
        netuid=netuid,
        cutoff_day=migration["cutoff_day"],
        cutoff_block=migration["cutoff_block"],
        cutoff_hash=migration["cutoff_hash"],
        entry_snapshot_sha256="a" * 64,
        entry_pairs=[],
        inventory=dict(
            pairs=[
                dict(
                    coldkey=r["coldkey"],
                    hotkey=r["hotkey"],
                    first_day=int(by_uid[r["uid"]]["firstActivityDay"]),
                    block=migration["cutoff_block"],
                    block_hash=migration["cutoff_hash"],
                )
                for r in rows
            ],
            counts=dict(earnings=len(rows), native=0, foreign=0),
            events_sha256="b" * 64,
        ),
        records=[
            dict(
                coldkey=r["coldkey"],
                hotkey=r["hotkey"],
                input={**copy.deepcopy(by_uid[r["uid"]]), "uid": None, "holdRao": "0"},
            )
            for r in rows
        ],
        foreign_valuations=[],
    )
    from noah.points.lineages import digest

    result["source_sha256"] = digest(result)
    return result


def for_context(fixture, context):
    migration = dict(
        genesis=context["genesis_hash"],
        cutoff_day=int(fixture["evalDay"]),
        cutoff_block=context["to_block"],
        cutoff_hash=context["to_block_hash"],
        miners=[
            dict(
                uid=row["uid"],
                coldkey=row["coldkey"],
                hotkey=context["scored_hotkeys"][str(row["uid"])],
            )
            for row in fixture["miners"]
            if row["uid"] is not None
        ],
    )
    return for_fixture(fixture, migration, netuid=context["netuid"])


def document(*, netuid=2, genesis="0x" + "11" * 32):
    """Small synthetic schema/2 policy for pure infrastructure validation tests."""
    import hashlib
    import json

    from noah.points.engine import POLICY
    from noah.points.lineages import lineage_state
    from noah.points.tests.test_engine import fixture

    raw = fixture([[(0, 0, 0)]])
    raw["miners"][0]["coldkey"] = "5" + "A" * 47
    row = raw["miners"][0]
    migration = dict(
        schema="noah-points-migration/1",
        policy=POLICY,
        genesis=genesis,
        cutoff_day=0,
        cutoff_block=100,
        cutoff_hash="0x" + "ab" * 32,
        source_sha256=hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
        consumed_receipt_ids=[],
        miners=[
            dict(uid=0, coldkey=row["coldkey"], hotkey="5" + "B" * 47, **lineage_state(raw, row))
        ],
    )
    return dict(
        schema="noah-points-policy/2",
        migration=migration,
        fixture=raw,
        lineages=for_fixture(raw, migration, netuid=netuid),
        historical_valuations=[],
        pricing=dict(
            method="previous-completed-utc-day-average",
            assets="all-subnet-tokens-and-root",
            credit="actual-received",
        ),
        ark_address="synthetic-ark",
        validator_hotkey="synthetic-validator",
    )
