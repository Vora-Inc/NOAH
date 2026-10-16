#!/usr/bin/env python3
"""Compare emitted amounts with the distribution represented by PerU16 storage.

Scale each stored share by the observed pool total and allow a per-seat tolerance
of pool_total_rao // 65535 + 2 for quantization. By default the pool total comes
from the emitted amounts: this tests distribution consistency, not independent
verification of the absolute emitted total. Inputs are already-decoded data.
Exit codes: 0 agreement, 1 mismatch, 2 no evidence or usage error."""

from __future__ import annotations

import json
import sys

#: PerU16's own scale -- the runtime's own fixed-point convention for this
#: storage item, not a parameter.
PER_U16_MAX = 65535


def expected_rao_by_seat(incentive_perU16_by_seat: dict, pool_total_rao: int) -> dict:
    """Each seat's real ``PerU16`` share of ``Incentive`` storage, scaled
    back into an expected rao amount by the epoch's own real pool total.

    Scaled by ``sum(incentive_perU16_by_seat.values())``, not a hardcoded
    65535: the stored shares need not sum to exactly 65535 (a seat with
    genuinely zero incentive contributes zero, and the runtime's own
    ``PerU16`` conversion carries its own rounding), so dividing by the
    ACTUAL total of what was read is the correct scale, not an assumed
    one -- the same real-total-not-65535 reasoning the staged reference
    reconciliation already worked out (``total_parts``, not
    ``PER_U16_MAX``, in the scaling division).
    """
    total_parts = sum(incentive_perU16_by_seat.values())
    if total_parts == 0:
        return {seat: 0 for seat in incentive_perU16_by_seat}
    return {
        seat: part * pool_total_rao // total_parts
        for seat, part in incentive_perU16_by_seat.items()
    }


def tolerance_rao(pool_total_rao: int) -> int:
    """The quantization bound a single seat's event-vs-storage delta may
    fall within and still agree: ``pool // 65535 + 2`` rao, covering the
    ``PerU16`` write's own rounding and this reconstruction's own integer
    division. Rows beyond this are real disagreements, not quantization
    noise."""
    return pool_total_rao // PER_U16_MAX + 2


def reconcile(event_amounts_by_seat: dict, incentive_perU16_by_seat: dict, *, pool_total_rao=None):
    """Compare the event's own per-seat rao amounts against
    ``Incentive`` storage's own per-seat ``PerU16`` shares, scaled to rao.

    ``event_amounts_by_seat``: ``{seat: amount_rao}``, the exact shape one
    mechanism of ``noah.chain.emissions.epoch_pay_vectors``'s own return
    value already is.

    ``incentive_perU16_by_seat``: ``{seat: perU16_value}``, decoded but
    otherwise unprocessed -- whatever a real ``SubtensorModule::Incentive``
    read (at the same packed ``netuid + mechanism * GLOBAL_MAX_SUBNET_COUNT``
    index ``noah.chain.emissions.netuid_and_mechanism`` already computes)
    would hand back once live access exists.

    ``pool_total_rao`` defaults to ``sum(event_amounts_by_seat.values())``
    -- the event's own vector is the one place this figure is cheap to
    read (no storage item exposes the pool total on its own; the staged
    reference reconciliation makes the identical choice). A caller may
    still override it, e.g. to cross-check against an independently read
    pool figure.

    Returns ``{"agree": bool, "pool_total_rao": int, "tolerance_rao": int,
    "rows": [...]}``, one row per seat named by EITHER side (a seat named
    by only one side is a row with the other side read as 0, exactly as
    real as any other disagreement -- never silently skipped).
    """
    if pool_total_rao is None:
        pool_total_rao = sum(event_amounts_by_seat.values())
    expected = expected_rao_by_seat(incentive_perU16_by_seat, pool_total_rao)
    tolerance = tolerance_rao(pool_total_rao)

    all_seats = sorted(set(event_amounts_by_seat) | set(expected))
    rows = []
    agree = True
    for seat in all_seats:
        event_rao = event_amounts_by_seat.get(seat, 0)
        storage_expected_rao = expected.get(seat, 0)
        delta_rao = event_rao - storage_expected_rao
        row_agrees = abs(delta_rao) <= tolerance
        agree = agree and row_agrees
        rows.append(
            {
                "seat": seat,
                "event_rao": event_rao,
                "storage_expected_rao": storage_expected_rao,
                "delta_rao": delta_rao,
                "tolerance_rao": tolerance,
                "agree": row_agrees,
            }
        )

    return {
        "agree": agree,
        "pool_total_rao": pool_total_rao,
        "tolerance_rao": tolerance,
        "rows": rows,
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 compare_incentive.py <reconciliation.json>", file=sys.stderr)
        return 2
    with open(argv[0], "r", encoding="utf-8") as fh:
        data = json.load(fh)

    event_amounts_by_seat = {
        int(seat): amount for seat, amount in data["event_amounts_by_seat"].items()
    }
    incentive_perU16_by_seat = {
        int(seat): value for seat, value in data["incentive_perU16_by_seat"].items()
    }

    report = reconcile(event_amounts_by_seat, incentive_perU16_by_seat)

    if not report["rows"]:
        print("NO EVIDENCE. Neither side named any seat, so they agree about nothing.")
        return 2

    print(f"pool total (rao)   {report['pool_total_rao']:,}")
    print(f"tolerance (rao)    {report['tolerance_rao']:,}")
    print(f"{'seat':>5} {'event (rao)':>16} {'storage expected (rao)':>24} {'delta':>10}  verdict")
    for row in report["rows"]:
        verdict = "agree" if row["agree"] else "DISAGREE"
        print(
            f"{row['seat']:>5} {row['event_rao']:>16,} {row['storage_expected_rao']:>24,} "
            f"{row['delta_rao']:>10,}  {verdict}"
        )

    if report["agree"]:
        print("\nAGREE, every seat within the PerU16 quantization bound.")
        return 0

    print("\nDISAGREE. At least one seat is beyond the quantization bound -- a real finding.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
