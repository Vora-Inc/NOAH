"""Authenticate a native position-transfer lineage using its actual receipt.

The existing decoder distinguishes ownership of transferred stake from ownership
of a hotkey. This proof is deliberately unavailable to other attribution kinds.
"""

import re


def validate(lineage, migration):
    proof = lineage["receipt"]
    if (
        not isinstance(proof, dict)
        or set(proof)
        != {
            "kind",
            "day",
            "event_ordinal",
            "extrinsic_index",
            "amount_rao",
            "destination_hotkey",
        }
        or proof["kind"] != "native-stake-and-hotkey-transfer"
        or any(
            type(proof[k]) is not int or proof[k] < 0
            for k in ("day", "event_ordinal", "extrinsic_index")
        )
        or not lineage["first_day"] <= proof["day"] <= migration["cutoff_day"]
        or not isinstance(proof["amount_rao"], str)
        or not re.fullmatch("[1-9][0-9]*", proof["amount_rao"])
        or not isinstance(proof["destination_hotkey"], str)
        or not re.fullmatch("[1-9A-HJ-NP-Za-km-z]{47,49}", proof["destination_hotkey"])
    ):
        raise ValueError("invalid native position-transfer lineage proof")
    receipt_id = f"{migration['genesis']}/{lineage['block_hash']}/{proof['event_ordinal']}"
    if (
        not isinstance(migration["consumed_receipt_ids"], list)
        or receipt_id not in migration["consumed_receipt_ids"]
    ):
        raise ValueError("lineage anchor receipt is not consumed by the migration")


def verify(chain, lineage, document, *, netuid, day_anchor_ts):
    from ..chain.deposits import deposits_in_block
    from ..chain.runner import _ParentBlockOwnerSource

    validate(lineage, document["migration"])
    proof = lineage["receipt"]
    block = lineage["block"]
    block_hash = chain.block_hash(block)
    timestamp = chain.block_timestamp(block)
    if (
        block_hash != lineage["block_hash"]
        or proof["destination_hotkey"] != document["validator_hotkey"]
        or type(timestamp) is not int
        or timestamp <= 0
        or (timestamp - day_anchor_ts) // 86400 != proof["day"]
    ):
        raise ValueError("native lineage receipt boundary or destination differs")
    receipts = deposits_in_block(
        chain.events(block),
        ark_coldkey=document["ark_address"],
        block=block,
        block_hash=block_hash,
        designated_validator_hotkey=document["validator_hotkey"],
        extrinsics=chain.extrinsics(block),
        parent_block_hash=chain.block_hash(block - 1),
        owner_source=_ParentBlockOwnerSource(chain, block - 1),
    )
    matches = [
        receipt
        for receipt in receipts
        if (
            receipt.carried_by == "StakeAndHotkeyTransferred"
            and receipt.token_netuid == netuid
            and receipt.coldkey == lineage["coldkey"]
            and receipt.origin_hotkey == lineage["hotkey"]
            and receipt.owner_at_deposit == lineage["coldkey"]
            and receipt.hotkey == proof["destination_hotkey"]
            and receipt.unattributed_reason is None
            and receipt.block == block
            and receipt.block_hash == block_hash
            and receipt.event_ordinal == proof["event_ordinal"]
            and receipt.extrinsic_index == proof["extrinsic_index"]
            and receipt.amount_rao == int(proof["amount_rao"])
        )
    ]
    if len(matches) != 1 or chain.block_hash(block) != block_hash:
        raise ValueError("native position-transfer lineage differs from finalized receipt")
