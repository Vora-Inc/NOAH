"""Execute one scoring period and report each stage's result.

Read pinned inputs, compare independent scoring engines, apply the keeper and seat
turnover rules, check prior publication when available, and use the configured
publication path. A proven prior mismatch halts; an unavailable prior check is
reported as unverified. The live adapter's ability to perform that check must be
assessed separately from this orchestration."""

from ..runtime_events import call as event_call
from .settings import SettingsError


class PeriodRefused(SettingsError):
    """The period was not published, and the reason is named."""


class AlreadyPublished(SettingsError):
    """This period already has a publication on chain."""


class HeightNotPinned(SettingsError):
    """The height to score at could not be fixed, so nothing was scored."""


def submission_vector(chain, height, agreed):
    """Compute the keeper decision and turnover-checked vector used for publication evidence."""
    from .keeper import decide
    from .vector import publishable_vector

    live_uids = chain.live_uids(height)
    owner_uids = chain.owner_uids()
    decision = decide(
        scored=agreed,
        live_uids=live_uids,
        owner_uids=owner_uids,
    )
    turnover_checked = publishable_vector(
        scored=decision.vector,
        live_uids=live_uids,
        fixture_hotkeys=chain.fixture_hotkeys(height),
        chain_hotkeys=chain.chain_hotkeys(),
        owner_uids=owner_uids,
    )
    return decision, turnover_checked


def run_period(chain):
    """Score one period and publish it, or refuse and say which step stopped it."""
    # 1. Pin the height before anything is read.
    try:
        height = chain.pinned_height()
    except Exception as error:
        raise HeightNotPinned(
            f"the height to score at could not be fixed ({type(error).__name__}: {error}), so "
            f"nothing was read. This does NOT fall back to the current head: every validator "
            f"must score the same state, and a validator reading its own height publishes "
            f"numbers nobody else agrees with while raising no error at all."
        ) from error

    if height is None:
        raise HeightNotPinned(
            "the height to score at came back as nothing, which is refused rather than "
            "treated as zero. A period scored at height zero would read an empty chain and "
            "publish an empty answer, and an empty answer is what a broken read looks like."
        )

    # 2. Before any work: has this period already been published?
    if chain.already_published(height):
        raise AlreadyPublished(
            f"the period at height {height} already has a publication on chain. Rescoring and "
            f"republishing it would spend the rate-limit window the NEXT period needs, so a "
            f"validator restarting mid-period could lock itself out of the one that matters."
        )

    # 3. Read and score at that height, then require both engines to agree.
    try:
        observations = event_call("validator", "validator.ingestion", chain.read, height,
                                  event_fields={"height": height})
    except Exception as error:
        raise PeriodRefused(
            f"the chain could not be read at height {height} ({type(error).__name__}: {error}). "
            f"Nothing was scored and nothing published."
        ) from error

    # Called on its own so a failure in scoring itself is reported as
    # "scoring failed" rather than folded into the twin proof's message
    # below. Its return value is not used; see the comment at decide() below
    # for why.
    try:
        event_call("validator", "validator.scoring", chain.score, observations,
                   event_fields={"height": height})
    except Exception as error:
        raise PeriodRefused(
            f"scoring failed at height {height} ({type(error).__name__}: {error}). Nothing "
            f"published."
        ) from error

    try:
        agreed = event_call("validator", "validator.agreement", chain.agree, observations,
                            event_fields={"height": height})
    except Exception as error:
        raise PeriodRefused(
            f"the two scoring engines did not agree at height {height} "
            f"({type(error).__name__}: {error}). Nothing is published, which is the point: "
            f"half of this validator's own arithmetic rejects the answer the other half "
            f"produced, so neither is fit to put on a chain."
        ) from error

    # Publish the vector validated by the independent agreement check. The
    # keeper validates values before turnover handling zeroes replaced seats;
    # reversing that order could hide an invalid value on a replaced seat.
    decision, turnover_checked = event_call(
        "validator", "validator.preparation", submission_vector, chain, height, agreed,
        event_fields={"height": height},
    )

    # Durable audit evidence is a pre-send gate on the real automatic adapter.
    # Stand-in adapters predating the capability keep their existing contract.
    record_calculation = getattr(chain, "record_calculation", None)
    if callable(record_calculation):
        try:
            event_call("validator", "validator.evidence", record_calculation,
                       height, observations, agreed, turnover_checked,
                       event_fields={"height": height})
        except Exception as error:
            raise PeriodRefused(
                f"calculation evidence could not be retained ({type(error).__name__}); "
                "nothing was submitted"
            ) from error

    # 5. Before adding a publication, check the last one actually landed as the
    #    rescale of what we sealed. A mismatch stops here; an unreadable check
    #    does not, and says so.
    from .landed import PreviousRevealMismatch, check_previous_reveal

    prior = check_previous_reveal(chain)
    if prior.halt:
        raise PreviousRevealMismatch(
            f"the period at height {height} was NOT published. {prior.reason}"
        )

    # 6. Publish, through whichever of the two paths this run is explicitly
    #    configured to use -- see this module's own docstring, "Two publish
    #    paths, chosen once, never guessed at", for the full reasoning.
    #    `chain` owns the choice (`publish_mode`), the same way it already
    #    owns `self.uid`/`self.wallet_hotkey`; this reads it via `getattr`
    #    with a "sealed" fallback so every fake chain this project's own
    #    tests already build, predating this attribute, keeps behaving
    #    exactly as it always did -- "sealed" IS today's only behavior.
    #
    #    The turnover-checked vector, not decision.vector, on EITHER path:
    #    decision.vector is what the engine's numbers validated to BEFORE a
    #    reassigned seat's weight was zeroed, and publishing it would undo
    #    the whole point of step 4's second half. Nothing below chooses a
    #    different vector or a different uid/hotkey per mode -- both paths
    #    publish this seat's own real computed vector, for the real
    #    configured uid, exactly as `chain.publish` alone always did.
    mode = getattr(chain, "publish_mode", "sealed")
    try:
        if mode == "direct":
            published = event_call("validator", "validator.publication", chain.publish_direct,
                                   turnover_checked, height, event_fields={"height": height})
        else:
            published = event_call("validator", "validator.publication", chain.publish,
                                   turnover_checked, height, event_fields={"height": height})
    except Exception as error:
        raise PeriodRefused(
            f"the period at height {height} was not published ({type(error).__name__}: "
            f"{error}). This does not retry: a period that failed failed for a reason, and "
            f"retrying by default turns one diagnosable failure into a rate-limit exhaustion "
            f"with no better explanation."
        ) from error

    record_submitted = getattr(chain, "record_submitted", None)
    submitted_record = (
        record_submitted(height, turnover_checked, published) if callable(record_submitted) else None
    )

    result = {
        "height": height,
        "seats": len(turnover_checked),
        "keeping_alive": decision.keeping_alive,
        "reason": decision.reason,
        "previous_verified": prior.verified,
        "previous_reason": prior.reason,
        "publish_mode": mode,
    }
    if submitted_record is not None:
        result["submitted_record"] = submitted_record
    if mode == "direct":
        # publish_weights_direct's own real return shape (noah.core.publish)
        # has neither "confirmed_on_chain" nor "reveal_round" -- there is no
        # seal and no reveal on this path, `set_mechanism_weights` applies in
        # the same block it lands in. Naming a "reveal_round" here would
        # fabricate a fact that does not exist for this publication, and
        # writing "confirmed_on_chain" from a differently-shaped confirmation
        # (`applied_weights_readback`, a per-seat block read-back rather than
        # a stored-commit-bytes match) would let a caller mistake one
        # meaning for the other. Both are surfaced under their own real
        # names instead, exactly as `publish_weights_direct` itself returns
        # them.
        result["applied_weights_readback"] = published.get("applied_weights_readback")
        result["block"] = published.get("block")
    else:
        result["confirmed_on_chain"] = published.get("confirmed_on_chain")
        result["reveal_round"] = published.get("reveal_round")
        result["block"] = published.get("block")
        result["extrinsic_index"] = published.get("extrinsic_index")
        record_commit = getattr(chain, "record_commit", None)
        if callable(record_commit):
            result["commit_ledger"] = record_commit(height, published)
    return result
