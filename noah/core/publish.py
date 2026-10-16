"""Publish a period's vector and verify available inclusion evidence.

Sealed publication checks for this identity's commit. Direct publication checks
this seat's applied weights. An acknowledgement from the sender alone is not
sufficient proof of chain inclusion, and uncertain outcomes must remain visible."""

from .commits import read_stored_commits
from .settings import SettingsError
from .startup import CannotPublishYet, check_can_publish_yet


class PublicationRefused(SettingsError):
    """Nothing was published, and the reason is ours rather than the chain's."""


class CommitRejected(SettingsError):
    """The chain refused the submission."""


class NotOnChain(SettingsError):
    """The submission reported success and the chain does not hold our bytes."""


# The chain takes u16 weights. keeper.py already checks this on its own output,
# but this is the actual sealing step, reachable by any caller whether or not
# it went through keeper.decide() first, so the check belongs here too rather
# than being assumed already done by whoever built ``vector``.
U16_MAX = 65535


def _validated_vector(vector):
    """Every seat's weight, checked exactly, in a fixed order.

    Shared by ``publish_weights`` and ``publish_weights_direct``: whether a
    weight is a real, in-range u16 has nothing to do with which of the two
    calls it is then submitted with, and this is the last checkpoint before
    EITHER one, reachable by any caller whether or not it went through
    ``keeper.decide()`` first.
    """
    uids = sorted(vector)
    for seat in uids:
        value = vector[seat]
        if type(value) is not int:
            raise PublicationRefused(
                f"seat {seat} carries {value!r} ({type(value).__name__}), not a plain "
                f"integer. Publishing it would submit a weight nobody actually computed as "
                f"one; nothing is submitted."
            )
        if not 0 <= value <= U16_MAX:
            raise PublicationRefused(
                f"seat {seat} carries {value}, outside 0 to {U16_MAX}. The chain takes "
                f"u16 weights and cannot accept this value."
            )
    values = [vector[seat] for seat in uids]
    return uids, values


def publish_weights(chain, *, vector, uid, netuid, hotkey):
    """Seal one vector, publish it, and confirm the chain holds it.

    There is deliberately no argument that skips the confirmation. A check that
    can be turned off is a check that will be, on the night it matters.
    """
    # 1. May this seat publish at all? Asked first, so a refusal costs nothing
    #    and no half-finished commit exists to be sent by mistake.
    try:
        check_can_publish_yet(chain, uid=uid, netuid=netuid)
    except CannotPublishYet as error:
        raise PublicationRefused(str(error)) from error

    # 2. Seal exactly what we were given. Every seat named, zeros included, in a
    #    fixed order so two validators sealing the same period agree.
    #
    #    Every value is checked before sealing, by exact type rather than
    #    `int(x)`: a float, a bool, a string that merely looks like a number,
    #    or a custom int subclass whose truth test lies about its own value
    #    (`type(value) is not int` catches all of those; `isinstance` alone
    #    would not, since bool and any other int subclass pass it) all mean a
    #    weight that was never validated upstream is about to be sealed and
    #    submitted. This is the last checkpoint before that happens, so it
    #    refuses rather than coercing. This check is shared, via
    #    _validated_vector, with the OTHER publish function this module
    #    defines below it: which call the weights are then submitted with
    #    does not change what makes one a real u16.
    uids, values = _validated_vector(vector)
    try:
        commit, reveal_round = chain.seal(uids, values, hotkey)
    except Exception as error:
        raise PublicationRefused(
            f"the vector could not be sealed ({type(error).__name__}: {error}), so nothing "
            f"was submitted. The period is unpublished and the validator has not spent its "
            f"rate-limit window."
        ) from error

    # 3. Submit.
    result = chain.submit(commit, reveal_round, netuid)
    if result.get("error") and result.get("indeterminate"):
        raise NotOnChain(
            f"the submission raised ({result['error']}) so it is not known whether the chain "
            f"accepted it. Treat the period as unconfirmed, not as refused: check the chain for "
            f"our commit before anything is published again."
        )
    if result.get("error"):
        raise CommitRejected(
            f"the chain refused this publication: {result['error']}. Nothing is on chain. "
            f"If the reason names a rate limit, a registration or a permit, it says nothing "
            f"about whether our numbers were right, only that this seat may not publish."
        )

    # 4. Read back. The submission's own success is not evidence.
    try:
        stored = list(read_stored_commits(chain, netuid))
    except Exception as error:
        raise NotOnChain(
            f"this publication was accepted and the chain could not then be read to confirm "
            f"it ({type(error).__name__}: {error}). Treat the period as unconfirmed rather "
            f"than published: an accepted submission and a stored commit are different facts."
        ) from error

    if commit in stored:
        return {
            "reveal_round": reveal_round,
            "confirmed_on_chain": True,
            "seats": len(uids),
            "block": result.get("block"),
            "extrinsic_index": result.get("extrinsic_index"),
        }

    if not stored:
        raise NotOnChain(
            "this publication was accepted and the commit storage reads empty. The reader "
            "has already checked that this is a real emptiness rather than a lost reader: "
            "the maps a wrong reader lands on were consulted and are empty too, so this is "
            "a statement about the chain and not only about the read. It still does not say "
            "the numbers were wrong. It says the chain accepted a submission and is not "
            "holding it, which needs a person before anything else is published."
        )

    raise NotOnChain(
        f"this publication was accepted and the chain holds {len(stored)} commit(s), none of "
        f"which is ours. The call succeeded and what arrived is not what we sent, which is "
        f"the failure this check exists for. Nothing further is published this period."
    )


def publish_weights_direct(chain, *, vector, uid, netuid, hotkey):
    """Submit the selected direct route and verify its exact finalized transaction."""
    try:
        check_can_publish_yet(chain, uid=uid, netuid=netuid)
    except CannotPublishYet as error:
        raise PublicationRefused(str(error)) from error
    uids, values = _validated_vector(vector)
    result = chain.submit_direct(uids, values, netuid)
    if result.get("indeterminate"):
        raise NotOnChain(
            f"direct publication outcome is unknown: {result.get('error')}. "
            "Reconcile the durable intent before another send."
        )
    if result.get("error"):
        raise CommitRejected(f"the chain refused this direct publication: {result['error']}")
    try:
        evidence = chain.confirm_direct(vector=vector, result=result)
    except Exception as error:
        raise NotOnChain(
            f"direct publication could not be confirmed ({type(error).__name__}: {error}). "
            "The send may have succeeded; reconcile before another send."
        ) from error
    if not evidence or evidence.get("confirmed") is not True:
        raise NotOnChain("no matching finalized direct transaction and exact applied weights")
    return {
        "applied_weights_readback": evidence,
        "seats": len(uids),
        "block": result.get("block"),
        "extrinsic_index": result.get("extrinsic_index"),
        "block_hash": evidence.get("block_hash"),
    }
