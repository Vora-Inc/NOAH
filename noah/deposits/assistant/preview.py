# ruff: noqa: E501
"""Plain-English text: the preview/confirmation summary and the status report."""

from .amounts import BELOW_MINIMUM, NOTHING_DUE, PARTIAL, READY, format_noah
from .ledger import (
    ABANDONED,
    APPROVED,
    AWAITING,
    CREDITED,
    DECLINED,
    FAILED,
    FINALIZED,
    SUBMITTED,
    SUBMITTING,
    UNCERTAIN,
    UNQUALIFIED,
)

MODE_TEXT = {
    "custom": "one-time custom amount",
    "fixed": "fixed amount",
    "percent": "percentage of eligible earnings",
    "maintenance": "contribution maintenance (only what is still needed)",
}


def render_summary(
    config, proposal, *, available_rao, evidence=None, fee_note=None, shape_text=None
):
    """The exact text a miner reads before approving. Shows Ark, validator and
    miner hotkey as three different things."""
    from .proxy_signing import signing
    signer = signing(config)
    proxy = signer['mode'] == 'proxy'
    fee_default = ('paid in TAO by the proxy delegate; see the public preflight estimate.' if proxy
                   else 'a network fee is paid in TAO from your coldkey. This tool does NOT estimate it.')
    lines = [
        "NOAH DEPOSIT PREVIEW (a preview sends nothing)",
        "",
        f"Network:                  {config.network} (subnet {config.netuid})",
        f"Your miner hotkey:        {config.miner_hotkey}",
        f"Your coldkey ({'owner' if proxy else 'signs'}):     {config.coldkey}",
        f"Official Ark (receives):  {config.ark_coldkey}",
        f"Designated Ark Validator: {config.validator_hotkey}",
        "  The stake must end up on the Ark Validator, not on your miner hotkey. A deposit on any",
        "  other validator is a real Ark receipt but earns no mining credit.",
        "",
        f"How the amount is chosen: {MODE_TEXT[proposal.mode]}",
    ]
    lines += [f"  {line}" for line in proposal.explanation]
    lines.append("")
    if proposal.status in (READY, PARTIAL):
        lines += [
            f"Proposed deposit:         {format_noah(proposal.amount_rao)}",
            f"Available to send now:    {format_noah(available_rao)}",
            f"Held back as reserve:     {format_noah(config.reserve_rao)}",
            f"Left after depositing:    {format_noah(available_rao - proposal.amount_rao)}",
            f"Network fee:              {fee_note or fee_default}",
        ]
        if shape_text:
            lines.append(f"How it is sent:           {shape_text}")
        lines.append(
            (f"Signing:                  proxy delegate {signer['proxy_address']} signs through the separate signer; the owner's key is not opened."
             if proxy else "Signing:                  you approve; a local bittensor wallet on this computer signs (no hardware-wallet path).")
        )
        lines.append(
            "Schedule:                 none. This is a manual run; nothing happens unless you run it."
        )
    elif proposal.status == NOTHING_DUE:
        lines.append("Result: nothing is due right now. No deposit will be sent.")
    elif proposal.status == BELOW_MINIMUM:
        lines.append("Result: below your minimum useful deposit. No deposit will be sent.")
    for warning in proposal.warnings:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)


_STATUS_TEXT = {
    AWAITING: "Awaiting your approval. Nothing has been sent.",
    APPROVED: "Approved by you; not yet signed or sent.",
    SUBMITTING: "Being sent. If this stays here after a crash, run status to check the chain.",
    SUBMITTED: "Submitted and included in a block. Submission alone does not prove credit.",
    FINALIZED: "Finalized on chain and read back as a qualifying deposit. Credit not yet confirmed.",
    CREDITED: "Qualifying credit confirmed by independent evidence.",
    UNCERTAIN: "Outcome unknown. No new deposit will start until this is reconciled with the chain.",
    UNQUALIFIED: "On chain but NOT counted as a qualifying deposit (see note). Money moved; do not repeat blindly.",
    DECLINED: "Not sent (declined or superseded).",
    FAILED: "Not sent; the chain or the signer rejected it. No deposit happened, but a network fee may have been charged.",
    ABANDONED: "Not on chain after the safe waiting period; treated as not sent.",
}


STATUS_LIMITS = (
    "Limits of this status: 'credit confirmed' means the credited total in your evidence rose by "
    "at least the deposit amount, so an unrelated credit of that size could confirm it early. "
    "A manual deposit made while an earlier one is being checked can confuse that check; the tool "
    "then holds or flags it and never sends again by itself."
)


def render_status(config, calculation, intents):
    lines = [f"Deposit status for miner {config.miner_hotkey}", ""]
    if calculation:
        lines.append(
            f"Calculated: {calculation.get('calculated_at')}: {calculation.get('headline')}"
        )
    else:
        lines.append("Calculated: no preview has been run yet.")
    if not intents:
        lines.append("No deposits have been started.")
    for row in intents[-5:]:
        lines.append(
            f"- {row['created_at']}  {format_noah(row['amount_rao'])}  "
            f"[{row['state'].replace('_', ' ')}]  {_STATUS_TEXT[row['state']]}"
        )
        if row.get("block_number") is not None:
            lines.append(f"    block {row['block_number']}  extrinsic {row.get('extrinsic_hash')}")
        if row.get("note"):
            lines.append(f"    note: {row['note']}")
    lines += ["", STATUS_LIMITS]
    return "\n".join(lines)
