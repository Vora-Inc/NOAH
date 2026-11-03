"""Build an epoch proposal from dual-sourced finalized facts and complete history.

The pending quantity is a forecast from the last realized incentive split. It is
not an on-chain spending limit. A changing Yuma consensus can differ from it.
"""

import json

from ..chain.collection import fingerprint
from ..chain.emissions import epoch_pay_vectors
from ..chain.history_check import confirm_history_contiguous
from ..chain.sources import Refusal
from ..core.calculation_record import verify
from .allocation import POLICY_ID
from .publication import artifact


def current_input(adapter, source_reference, head):
    root = adapter._submitted_record_dir
    manifest = verify(root, source_reference)
    fixture = artifact(root, manifest, "fixture")
    context = artifact(root, manifest, "context")
    chain = adapter.runner_chain
    if manifest["genesis_hash"] != chain.genesis_hash() or manifest["netuid"] != adapter.netuid:
        raise Refusal("ordinary evidence belongs to another chain or subnet")
    if (
        context.get("ark_address") != adapter.ark_coldkey
        or context.get("designated_validator_hotkey") != adapter.designated_validator_hotkey
        or int(fixture["dayEpochTs"]) != adapter.day_anchor_ts
        or context.get("scoring_dials") != adapter.dials
        or context.get("day_start_utc_unix") != adapter.day_anchor_ts + manifest["day"] * 86400
    ):
        raise Refusal("ordinary score configuration differs from starter reader")
    now = chain.block_timestamp(head)
    day = (now - adapter.day_anchor_ts) // 86400
    if int(fixture["evalDay"]) != day - 1:
        raise Refusal("starter allocation needs the latest closed-day score")
    if chain.block_hash(context["to_block"]) != context["to_block_hash"]:
        raise Refusal("ordinary score finalized boundary changed")
    info = chain.starter_epoch_info(head)
    if info["consensus"] != "Yuma" or chain.mechanism_count(head) != 1:
        raise Refusal("starter reader currently requires one-mechanism Yuma")
    epoch = info["LastEpochBlock"]
    if epoch <= 0 or info["LastMechansimStepBlock"] != epoch:
        raise Refusal("latest epoch did not settle successfully")
    if info["LastYumaStepBlock"] != epoch:
        # Switching consensus does not retroactively turn the last Null payout
        # into a Yuma sample. The setter can also follow an epoch in the same
        # block, so consensus at that block alone is not proof of Yuma execution.
        raise Refusal("latest settled epoch was not Yuma; wait for Yuma settlement")
    tempo = info["Tempo"]
    next_epoch = epoch + tempo
    if info["PendingEpochAt"]:
        next_epoch = min(next_epoch, info["PendingEpochAt"])
    if tempo <= 0 or head >= next_epoch or info["BlocksSinceLastStep"] > tempo:
        raise Refusal("epoch is disabled, due, or deferred; wait for finalized settlement")
    previous = chain.starter_epoch_info(epoch - 1)["LastMechansimStepBlock"]
    epoch_start = chain.block_timestamp(epoch)
    seconds = epoch_start - chain.block_timestamp(previous)
    if previous <= 0 or seconds <= 0:
        raise Refusal("two settled epochs are required to measure the mining pool")
    seats = chain.seat_hotkeys(head)
    scored = chain.seat_hotkeys(context["to_block"])
    rows = [m for m in fixture["miners"] if m["uid"] is not None]
    selected = {int(m["uid"]): scored.get(int(m["uid"])) for m in rows}
    if any(hot is None or seats.get(uid) != hot for uid, hot in selected.items()):
        raise Refusal("scored miner identity changed; require a fresh daily score")
    owners = chain.hotkey_owners(list(selected.values()), head)
    if any(owners[selected[int(m["uid"])]] != m["coldkey"] for m in rows):
        raise Refusal("scored coldkey ownership changed")
    vectors = epoch_pay_vectors(
        chain.events(epoch),
        netuid=adapter.netuid,
        mechanism_count=1,
        hotkey_by_uid=chain.seat_hotkeys(epoch),
        subnetwork_n=chain.subnetwork_n(epoch),
        block=epoch,
    )
    pool = sum(vectors.get(0, {}).values())
    if pool <= 0:
        raise Refusal("no positive mining payout observed at latest epoch")
    incentive = chain.incentive(head)
    if len(incentive) != chain.subnetwork_n(head) or not sum(incentive):
        raise Refusal("pending-reward forecast requires a complete positive incentive vector")
    store = adapter._store_factory()
    try:
        registration = info["NetworkRegisteredAt"]
        if not 0 < registration <= head:
            raise Refusal("subnet registration history boundary unavailable")
        first_day = (chain.block_timestamp(registration) - adapter.day_anchor_ts) // 86400
        confirm_history_contiguous(store, through_day=day - 1, from_day=first_day)
        row = store._conn.execute(
            "SELECT body FROM epoch_collection WHERE day=?", (day,)
        ).fetchone()
        cursor = json.loads(row[0]) if row else {}
        previous_last = dict((d, high) for d, low, high in store.scanned_days())[day - 1]
        expected_settings = fingerprint(
            dict(
                version=1,
                genesis=chain.genesis_hash(),
                netuid=adapter.netuid,
                ark=adapter.ark_coldkey,
                validator=adapter.designated_validator_hotkey,
                holding_stride=adapter._holding_stride,
                read_collateral=adapter._read_collateral,
                attribute_deposits=True,
            )
        )
        if (
            cursor.get("last") != head
            or cursor.get("first") != previous_last + 1
            or cursor.get("settings") != expected_settings
            or cursor.get("first_hash") != chain.block_hash(cursor["first"])
            or cursor.get("last_hash") != chain.block_hash(head)
        ):
            raise Refusal("intraday collection range, identity or finalized hashes do not match")
        # Include departed hotkeys and all earlier owners. SQL SUM can overflow;
        # Python integers preserve the complete lifetime total without truncation.
        totals = {}
        for cold, hot, amount in store._conn.execute(
            "SELECT coldkey,hotkey,amount_rao FROM attributed_pay WHERE block<=?", (head,)
        ):
            totals[cold, hot] = totals.get((cold, hot), 0) + amount
    finally:
        store.close()
    pending = {}
    # Reserve one forecast epoch even if an overlay should land before it. This
    # deliberately pauses near-limit starters rather than spending pending credit.
    all_owners = chain.hotkey_owners(list(seats.values()), head)
    for uid, hot in seats.items():
        cold = all_owners[hot]
        pending[cold, hot] = (pool * incentive[uid] + sum(incentive) - 1) // sum(incentive)
    for m in rows:
        totals.setdefault((m["coldkey"], selected[int(m["uid"])]), 0)
    all_pairs = set(totals) | set(pending)
    excluded = adapter.owner_uids()
    return dict(
        schema="noah-starter-epoch-preview/4",
        policyId=POLICY_ID,
        fixture=fixture,
        scoredHotkeys={str(uid): hot for uid, hot in selected.items()},
        lifetimeEarnings=[
            dict(
                coldkey=c,
                hotkey=h,
                earnedRao=totals.get((c, h), 0),
                pendingExpectedRao=pending.get((c, h), 0),
            )
            for c, h in sorted(all_pairs)
        ],
        snapshot=dict(
            genesisHash=chain.genesis_hash(),
            netuid=adapter.netuid,
            epoch=epoch,
            finalizedBlock=head,
            finalizedHash=chain.block_hash(head),
            epochSeconds=seconds,
            epochStartTimestamp=epoch_start,
            nextEpochBlock=next_epoch,
            finalizedTimestamp=now,
            miningPoolSample=dict(
                amountRao=pool, durationSeconds=seconds, endTimestamp=epoch_start
            ),
            weightEffectiveEpoch=next_epoch,
            pendingThroughEpoch=next_epoch - 1,
            seats=[
                dict(
                    uid=int(m["uid"]),
                    coldkey=m["coldkey"],
                    hotkey=selected[int(m["uid"])],
                    observedBlock=head,
                    spendableEarnedRao=totals[m["coldkey"], selected[int(m["uid"])]],
                    pendingExpectedRao=pending.get((m["coldkey"], selected[int(m["uid"])]), 0),
                    ownerExcluded=int(m["uid"]) in excluded,
                )
                for m in rows
            ],
        ),
    )
