"""Pinned entry-policy transition used by the real daily adapter.

The migration keeps historical ten-NOAH credit. It removes the entry purchase
requirement after the cutoff without changing any ordinary scoring arithmetic.
"""

import json
from pathlib import Path

from ..chain.sources import Refusal
from .entry_migration import LEGACY_ENTRY, FrozenEntries
from .migration_fields import digest, fields, integer


class EntryPolicyRefused(Refusal):
    pass


class EntryPolicy:
    def __init__(self, snapshot):
        fields(snapshot, "payload sha256", "entry snapshot")
        payload = snapshot["payload"]
        fields(
            payload,
            "schema genesisHash netuid finalizedBlock finalizedHash asOfDay legacyEntryRao records",
            "entry payload",
        )
        if (
            snapshot["sha256"] != digest(payload)
            or payload["schema"] != "noah-frozen-entry/1"
            or payload["legacyEntryRao"] != LEGACY_ENTRY
        ):
            raise EntryPolicyRefused("invalid entry migration digest or policy")
        integer(payload["asOfDay"], "cutoff day")
        integer(payload["finalizedBlock"], "cutoff block", 1)
        if not isinstance(payload["records"], list):
            raise EntryPolicyRefused("entry records must be a list")
        # The full receipt and chain checks require the adapter's reader/store.
        self.snapshot = json.loads(json.dumps(snapshot))

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def inputs(self, adapter, store, seats, day, to_block):
        from ..core.chain_adapter import _registration_days_for

        payload = self.snapshot["payload"]
        if adapter.entry_deposit_rao != LEGACY_ENTRY:
            raise EntryPolicyRefused("legacy entry recognition must remain ten NOAH")
        if day <= payload["asOfDay"]:
            # Historical replay uses the old rules up to and including cutoff.
            return None
        chain = adapter.runner_chain
        block = payload["finalizedBlock"]
        if block > to_block or chain.block_hash(block) != payload["finalizedHash"]:
            raise EntryPolicyRefused("entry migration finalized block does not match chain")
        cutoff_day = (chain.block_timestamp(block) - adapter.day_anchor_ts) // 86400
        if cutoff_day != payload["asOfDay"]:
            raise EntryPolicyRefused("entry migration block is outside its cutoff day")
        if (
            block >= to_block
            or (chain.block_timestamp(block + 1) - adapter.day_anchor_ts) // 86400 != cutoff_day + 1
        ):
            raise EntryPolicyRefused("entry migration must pin the last block of its cutoff day")
        cutoff_seats = chain.seat_hotkeys(block)
        owners = chain.hotkey_owners(list(cutoff_seats.values()), block)
        expected = {(owners[hot], hot) for hot in cutoff_seats.values()}
        actual = {(r["coldkey"], r["hotkey"]) for r in payload["records"]}
        if actual != expected:
            raise EntryPolicyRefused("migration must include every identity at the cutoff")
        cutoff_mapping = {}
        for uid, hot in cutoff_seats.items():
            cutoff_mapping.setdefault(owners[hot], {})[hot] = uid
        cutoff_registration = _registration_days_for(chain, store, cutoff_mapping, block)
        if any(
            cutoff_registration.get(row["coldkey"], {}).get(row["hotkey"]) != row["registrationDay"]
            for row in payload["records"]
        ):
            raise EntryPolicyRefused(
                "migration registration days differ from chain facts at cutoff"
            )
        registration_days = _registration_days_for(chain, store, seats, to_block)
        frozen = FrozenEntries(
            store,
            self.snapshot,
            genesis_hash=chain.genesis_hash(),
            netuid=adapter.netuid,
            registration_days=registration_days,
            as_of_day=day,
        )
        for cold, rows in registration_days.items():
            for hot, registered in rows.items():
                if registered <= payload["asOfDay"] and (cold, hot) not in actual:
                    raise EntryPolicyRefused("pre-cutoff identity missing from migration")
        return frozen, registration_days, frozen.credited, frozen.entry_days


def selected(adapter):
    """Historical accounting view; it never allocates rewards or runs a guard."""
    return getattr(adapter, "entry_history_policy", None) or getattr(
        adapter, "starter_entry_policy", None
    )


def configure(adapter, settings):
    from ..core.settings import SettingsError

    # Keep the existing filename/config key so the authenticated snapshot is
    # reused byte-for-byte. This is historical credit, not pool configuration.
    path = getattr(settings, "starter_entry_snapshot", None)
    if not path:
        raise SettingsError("ordinary points requires the frozen historical entry snapshot")
    if not (
        settings.epoch_collection
        and settings.epoch_republication
        and settings.publish_mode == "direct"
    ):
        raise SettingsError("ordinary points requires direct publication and both epoch features")
    if adapter._publish_intent is None or adapter._submitted_record_dir is None:
        raise SettingsError("ordinary points requires durable intent and calculation storage")
    try:
        adapter.entry_history_policy = EntryPolicy.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SettingsError("historical entry snapshot invalid") from exc
    adapter.starter_entry_policy = None
