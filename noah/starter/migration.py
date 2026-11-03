"""Immutable offline entry-credit migration. No production adapter is activated.

Freeze the original ten-token allocations BEFORE changing eligibility. Later
calculations reuse the frozen allocation and qualification day, never a changed
entry threshold. Missing historical evidence refuses the entire freeze.
"""

import json
from pathlib import Path

from .allocation import digest, fields, integer, text

LEGACY_ENTRY = 10_000_000_000


def freeze(
    store, registration_days, *, genesis_hash, netuid, finalized_block, finalized_hash, as_of_day
):
    # Existing attribution and scanning checks, without reimplementing receipts.
    from ..core.chain_adapter import _entry_allocation

    integer(as_of_day, "migration day")
    integer(netuid, "netuid")
    integer(finalized_block, "migration block")
    text(genesis_hash, "genesis")
    text(finalized_hash, "block hash")
    records = []
    for cold, seats in sorted(registration_days.items()):
        for hot, registered in sorted(seats.items()):
            text(cold, "coldkey")
            text(hot, "hotkey")
            integer(registered, "registration day")
            # Even a never-seen hotkey needs affirmative scanned-day evidence.
            # _entry_allocation alone returns empty when first activity is absent.
            for day in range(registered, as_of_day + 1):
                store.noah_deposits_for_hotkey(cold, hot, day)
            first = store.first_activity_day_for_hotkey(cold, hot)
            allocation = {}
            if registered <= as_of_day and (first is None or first >= registered):
                allocation = _entry_allocation(
                    store, cold, hot, LEGACY_ENTRY, through_day=as_of_day
                )
            records.append(
                dict(
                    coldkey=cold,
                    hotkey=hot,
                    registrationDay=registered,
                    amountRao=sum(allocation.values()),
                    allocation={str(k): v for k, v in sorted(allocation.items())},
                )
            )
    payload = dict(
        schema="noah-frozen-entry/1",
        genesisHash=genesis_hash,
        netuid=netuid,
        finalizedBlock=finalized_block,
        finalizedHash=finalized_hash,
        asOfDay=as_of_day,
        legacyEntryRao=LEGACY_ENTRY,
        records=records,
    )
    return dict(payload=payload, sha256=digest(payload))


def save_new(path, snapshot):
    """Exclusive creation: never replace a historical migration artifact."""
    with Path(path).open("x") as handle:
        json.dump(snapshot, handle, sort_keys=True, indent=2)
        handle.write("\n")


class FrozenEntries:
    """Read-only store view plus preserved engine inputs, pinned to original receipts."""

    def __init__(self, store, snapshot, *, genesis_hash, netuid, registration_days, as_of_day):
        from ..core.chain_adapter import _entry_allocation

        fields(snapshot, "payload sha256", "frozen entry envelope")
        payload = snapshot["payload"]
        fields(
            payload,
            "schema genesisHash netuid finalizedBlock finalizedHash asOfDay legacyEntryRao records",
            "frozen entry payload",
        )
        if snapshot["sha256"] != digest(payload):
            raise ValueError("frozen entry digest mismatch")
        if (
            payload["schema"] != "noah-frozen-entry/1"
            or payload["genesisHash"] != genesis_hash
            or payload["netuid"] != netuid
            or payload["legacyEntryRao"] != LEGACY_ENTRY
        ):
            raise ValueError("frozen entry identity/version mismatch")
        integer(payload["asOfDay"], "frozen day")
        integer(as_of_day, "evaluation day")
        integer(payload["finalizedBlock"], "frozen block")
        text(payload["finalizedHash"], "frozen hash")
        if as_of_day < payload["asOfDay"]:
            raise ValueError("cannot apply migration before its cutoff")
        if not isinstance(payload["records"], list):
            raise ValueError("entry records must be an array")
        self._store, self._seat, self._wallet = store, {}, {}
        self._registration_starts = {
            cold: max(
                payload["asOfDay"] + 1,
                min(integer(day, "registration day") for day in days.values()),
            )
            for cold, days in registration_days.items() if days
        }
        self.credited, self.entry_days = {}, {}
        seen = set()
        for row in payload["records"]:
            fields(row, "coldkey hotkey registrationDay amountRao allocation", "entry record")
            cold, hot = text(row["coldkey"], "coldkey"), text(row["hotkey"], "hotkey")
            pair = (cold, hot)
            if pair in seen:
                raise ValueError("duplicate frozen entry identity")
            seen.add(pair)
            registered = integer(row["registrationDay"], "registration day")
            amount = integer(row["amountRao"], "entry amount")
            allocation = row["allocation"]
            if not isinstance(allocation, dict) or amount not in (0, LEGACY_ENTRY):
                raise ValueError("invalid frozen allocation")
            first = store.first_activity_day_for_hotkey(cold, hot)
            expected = {}
            for day in range(registered, payload["asOfDay"] + 1):
                store.noah_deposits_for_hotkey(cold, hot, day)
            if registered <= payload["asOfDay"] and (first is None or first >= registered):
                expected = _entry_allocation(
                    store, cold, hot, LEGACY_ENTRY, through_day=payload["asOfDay"]
                )
            if allocation != {str(k): v for k, v in expected.items()}:
                raise ValueError("frozen allocation not supported by receipts at original cutoff")
            days = []
            for key, consumed in allocation.items():
                if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
                    raise ValueError("noncanonical allocation day")
                day = int(key)
                integer(consumed, "consumed entry", 1)
                if not registered <= day <= payload["asOfDay"]:
                    raise ValueError("entry outside migration interval")
                if store.noah_deposits_for_hotkey(cold, hot, day) < consumed:
                    raise ValueError("frozen entry no longer supported by receipts")
                self._seat[cold, hot, day] = consumed
                self._wallet[cold, day] = self._wallet.get((cold, day), 0) + consumed
                days.append(day)
            if sum(allocation.values()) != amount:
                raise ValueError("entry amount differs from frozen allocation")
            # A replacement registration must not receive the old starting credit.
            if amount and registration_days.get(cold, {}).get(hot) == registered:
                self.credited.setdefault(cold, {})[hot] = amount
                self.entry_days.setdefault(cold, {})[hot] = max(days)

    def noah_deposits_for_hotkey(self, coldkey, hotkey, day):
        observed = self._store.noah_deposits_for_hotkey(coldkey, hotkey, day)
        remaining = observed - self._seat.get((coldkey, hotkey, day), 0)
        if remaining < 0:
            raise ValueError("entry receipts changed after validation")
        return remaining

    def first_activity_day(self, coldkey):
        # A verified registration admits a never-paid, never-funded newcomer
        # starting no earlier than this policy's activation day.
        # Preserve the existing first activity of every established wallet;
        # the fallback adds no fictitious receipt, entry credit or payment.
        observed = self._store.first_activity_day(coldkey)
        return observed if observed is not None else self._registration_starts.get(coldkey)

    def noah_deposits(self, coldkey, day):
        remaining = self._store.noah_deposits(coldkey, day) - self._wallet.get((coldkey, day), 0)
        if remaining < 0:
            raise ValueError("wallet receipts changed after validation")
        return remaining

    def __getattr__(self, name):
        return getattr(self._store, name)
