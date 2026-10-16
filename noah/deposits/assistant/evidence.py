"""Read earnings and credited-contribution evidence for deposit proposals.

Incomplete or unavailable history is distinct from zero earnings. File evidence is
operator supplied; store readers follow the interface expected by this module."""

import json
from dataclasses import fields

from ...chain.sources import Refusal
from ...pathtext import plain_path_text
from .amounts import DepositRefused, Evidence

_INT_FIELDS = (
    "period_eligible_earnings_rao",
    "cumulative_eligible_earnings_rao",
    "credited_contributions_rao",
    "entry_contribution_rao",
)


class JsonFileEvidence:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise DepositRefused(
                f"The earnings evidence file {plain_path_text(self.path)} could not be read "
                f"({type(exc).__name__}). Without it no "
                f"amount can be calculated.",
                "missing_evidence",
            ) from None
        if not isinstance(raw, dict):
            raise DepositRefused("The evidence file must hold one JSON object.", "missing_evidence")
        known = {f.name for f in fields(Evidence)}
        unknown = set(raw) - known
        if unknown:
            raise DepositRefused(
                f"The evidence file has unknown fields {sorted(unknown)}.", "bad_input"
            )
        values = dict(raw)
        for name in _INT_FIELDS:
            if values.get(name) is not None:
                try:
                    values[name] = int(values[name])
                except (TypeError, ValueError):
                    raise DepositRefused(
                        f"Evidence field {name} is not a whole number.", "bad_input"
                    ) from None
        values.setdefault("source", f"file:{plain_path_text(self.path)}")
        return Evidence(**values)


class StoreEvidence:
    """Evidence from a validator history store (see module docstring)."""

    def __init__(
        self,
        *,
        store,
        seats,
        coldkey,
        hotkey,
        entry_deposit_day,
        through_day,
        previous_through_day,
        emissions_reader,
        deposited_reader,
    ):
        self.args = dict(store=store, seats=seats, coldkey=coldkey, hotkey=hotkey)
        self.entry_deposit_day = entry_deposit_day
        self.through_day = through_day
        self.previous_through_day = previous_through_day
        self.emissions_reader = emissions_reader
        self.deposited_reader = deposited_reader

    def read(self):
        a = self.args
        try:
            cumulative = self.emissions_reader(
                a["store"],
                a["seats"],
                a["hotkey"],
                entry_deposit_day=self.entry_deposit_day,
                eval_day=self.through_day,
            )
            previous = (
                self.emissions_reader(
                    a["store"],
                    a["seats"],
                    a["hotkey"],
                    entry_deposit_day=self.entry_deposit_day,
                    eval_day=self.previous_through_day,
                )
                if self.previous_through_day
                else 0
            )
            through_total = self.deposited_reader(
                a["store"], a["coldkey"], a["hotkey"], through_day=self.through_day
            )
            entry_total = self.deposited_reader(
                a["store"], a["coldkey"], a["hotkey"], through_day=self.entry_deposit_day
            )
        except Refusal as refusal:
            raise DepositRefused(
                f"History is not available yet: {refusal}", "missing_evidence"
            ) from None
        return Evidence(
            through_day=str(self.through_day),
            history_complete=True,
            source="validator-store",
            period_start_day=str(self.previous_through_day or self.entry_deposit_day),
            period_eligible_earnings_rao=cumulative - previous,
            cumulative_eligible_earnings_rao=cumulative,
            credited_contributions_rao=through_total - entry_total,
            entry_contribution_rao=entry_total,
        )
