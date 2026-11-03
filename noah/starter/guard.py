"""Offline watchdog decision. Does not sign, send, or expire on-chain weights.

A production caller must serialize with the real publisher, reconcile prior
intents, submit the fallback, and prove its effect at a finalized epoch. Running
this function alone provides NO protection from an updater outage.
"""

from .allocation import digest, integer, preview


def decision(raw, previous, *, now_timestamp, observed_weights):
    """Recompute rather than trust an edited output artifact; never reuse old overlays.

    `observed_weights` must be the finalized own-validator weight row supplied
    by the verified reader, not a local last-submitted guess.
    """
    integer(now_timestamp, "watchdog timestamp")
    if not isinstance(observed_weights, dict) or any(
        not isinstance(k, str) or not k.isdecimal() or type(v) is not int or not 0 <= v <= 65535
        for k, v in observed_weights.items()
    ):
        raise ValueError("invalid observed weight row")
    result = preview(raw, previous)
    observed_at = raw["snapshot"]["finalizedTimestamp"]
    if now_timestamp < observed_at:
        raise ValueError("watchdog clock precedes observation")
    expired = now_timestamp >= result["validUntilTimestamp"]
    desired = result["ordinaryFallbackWeightsU16"] if expired else result["proposedWeightsU16"]
    # All-zero publication can invoke chain-specific fallback behavior. Never
    # invent an owner/burn/equal allocation or pretend this case is safe.
    daily_end = int(raw["fixture"]["dayEpochTs"]) + (int(raw["fixture"]["evalDay"]) + 1) * 86400
    if now_timestamp >= daily_end + 86400:
        action = "blocked-fresh-ordinary-score-required"
        desired = None
    elif not sum(desired.values()):
        action = "blocked-no-ordinary-weight"
    elif desired == observed_weights:
        action = "no-change"
    else:
        action = "replace-with-ordinary" if expired else "replace-with-starter-overlay"
    return dict(
        offlineOnly=True,
        action=action,
        desiredWeightsU16=desired,
        inputHash=digest(raw),
        expired=expired,
        requiresFinalizedReplacementProof=action.startswith("replace"),
        liveProtection=False,
        warning=(
            "A decision is not a submission. "
            "Old on-chain weights continue until replaced or made inactive."
        ),
    )
