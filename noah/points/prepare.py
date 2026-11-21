"""Build an immutable, unsigned points policy from retained finalized evidence.

No chain writes or wallet imports. Startup independently checks the chain pins,
roster and balances again through both configured readers.
"""

import argparse
import hashlib
import json
from pathlib import Path

from ..core.calculation_record import durable_write, encoded, verify
from ..scoring.engine import score_fixture, score_miner
from ..scoring.fixture_io import parse_fixture
from .engine import POLICY, evaluate
from .migration import validate


def build(directory, reference, pricing, consumed_receipt_ids):
    evidence = verify(directory, reference)

    def read(name):
        return json.loads((Path(directory) / evidence["artifacts"][name]["path"]).read_text())

    f, c, score = read("fixture"), read("context"), read("agreed_score")
    if f.get("schema") != "noah-fixture/6" or score.get("schema") != "noah-score-output/6":
        raise ValueError("migration requires the existing production formula snapshot")
    if c.get("source_agreement") != "required_by_ingestion" or not c.get("scored_hotkeys"):
        raise ValueError(
            "finalized dual-reader identities required; recollect snapshot with this candidate"
        )
    if pricing != {
        "method": "previous-completed-utc-day-average",
        "assets": "all-subnet-tokens-and-root",
        "credit": "actual-received",
    }:
        raise ValueError("unsupported daily reference policy")
    fx = parse_fixture(f)
    if score_fixture(fx) != score:
        raise ValueError("retained legacy score does not reproduce from its fixture")
    rows = evaluate(f)["miners"]
    legacy = {m.uid: score_miner(m, fx.dials, fx.eval_day) for m in fx.miners}
    m = dict(
        schema="noah-points-migration/1",
        policy=POLICY,
        genesis=c["genesis_hash"],
        cutoff_hash=c["to_block_hash"],
        cutoff_block=c["to_block"],
        cutoff_day=int(f["evalDay"]),
        source_sha256=hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest(),
        consumed_receipt_ids=consumed_receipt_ids,
        miners=[],
    )
    for r in rows:
        old = legacy[r["uid"]]
        m["miners"].append(
            dict(
                uid=r["uid"],
                coldkey=r["coldkey"],
                hotkey=c["scored_hotkeys"][str(r["uid"])],
                state={
                    **{k: int(v) for k, v in r["state"].items()},
                    "balance": old.coverage_reserve_rao - old.debt_rao,
                },
                legacy_coverage_rao=old.coverage_reserve_rao,
                legacy_debt_rao=old.debt_rao,
            )
        )
    validate(m, expected_genesis=c["genesis_hash"], expected_cutoff_hash=c["to_block_hash"])
    from .lineages import POLICY_SCHEMA, validate_document
    capture = c.get("points_lineage_capture")
    document = dict(
        schema=POLICY_SCHEMA,
        lineages=capture,
        migration=m,
        fixture=f,
        historical_valuations=c.get("foreign_valuations", []),
        pricing=pricing,
        ark_address=c["ark_address"],
        validator_hotkey=c["designated_validator_hotkey"],
    )
    validate_document(document, netuid=c["netuid"])
    return document


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-dir", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--pricing", type=Path, required=True)
    p.add_argument("--receipt-ids", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    result = build(
        a.evidence_dir,
        json.loads(a.reference.read_text()),
        json.loads(a.pricing.read_text()),
        json.loads(a.receipt_ids.read_text()),
    )
    durable_write(a.out, encoded(result))
    print(
        json.dumps(
            dict(path=str(a.out), identities=len(result["migration"]["miners"]), signing=False)
        )
    )


if __name__ == "__main__":
    main()
