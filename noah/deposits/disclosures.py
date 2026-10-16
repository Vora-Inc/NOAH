"""The five notices shown before a depositor approves a transaction.

Keep participant-facing explanations in docs/DEPOSIT_NOTICES.md consistent with
these notices. Acknowledgment is required for all five topics before building.
"""

from dataclasses import dataclass

from ..chain.sources import Refusal

# Participant wording maintained with docs/DEPOSIT_NOTICES.md.
# This flag describes the copy, not launch readiness or permission to send.
WORDING_IS_PLACEHOLDER = False

REQUIRED_DISCLOSURES = frozenset(
    {
        "permanence",
        "governance_exception",
        "no_custody",
        "rail_caveat",
        "no_return",
    }
)

_DISCLOSURE_TEXT = {
    "permanence": """
        Treat this deposit as permanent. It transfers ownership of your stake to
        the Ark. Noah provides no withdrawal or refund process.
    """,
    "governance_exception": """
        Deposits remain subject to Bittensor's chain rules. Future changes to
        those rules or governance decisions could affect the Ark and its holdings.
    """,
    "no_custody": """
        The helper builds a transaction for your approval. Your configured wallet
        signs it on your computer, and the chain processes it. Do not give Noah
        your recovery phrase or private keys.
    """,
    "rail_caveat": """
        Transfers must be enabled for the token you deposit. Check the network,
        Ark receiving address, validator delegation and amount before signing.
    """,
    "no_return": """
        The Ark does not repay your deposit or promise you a financial return.
        Qualifying contributions can affect your Noah mining score. Rewards and
        continued ownership of a miner seat are not guaranteed.
    """,
}


class DisclosuresNotAcknowledged(Refusal):
    """Some required disclosure was not acknowledged, or the acknowledgment is not one."""


@dataclass(frozen=True)
class Acknowledgment:
    """Proof that every required disclosure was acknowledged.

    A frozen topic set normally produced by :func:`acknowledge`. Builders
    validate it again with :func:`require_acknowledgment`.
    """

    topics: frozenset


def disclosure_text(topic):
    """Return the participant notice for one required topic."""
    if topic not in REQUIRED_DISCLOSURES:
        raise DisclosuresNotAcknowledged(
            f"{topic!r} is not a disclosure this tool knows. The five required topics are "
            f"{sorted(REQUIRED_DISCLOSURES)}."
        )
    return " ".join(_DISCLOSURE_TEXT[topic].split())


def acknowledge(topics):
    """An acknowledgment covering every required disclosure, or a refusal.

    Named topics rather than a count, because acknowledging five things is not
    the same as acknowledging the five things, and a misspelling would otherwise
    make up the number while leaving a real disclosure unread.
    """
    if not isinstance(topics, (set, frozenset)):
        raise DisclosuresNotAcknowledged(
            f"acknowledgment needs the set of topics that were shown and accepted, got {topics!r}."
        )
    unknown = set(topics) - REQUIRED_DISCLOSURES
    if unknown:
        raise DisclosuresNotAcknowledged(
            f"these are not disclosures this tool knows: {sorted(unknown)}. A caller "
            f"inventing a topic has misunderstood the list, and the list is exactly what "
            f"the depositor is being asked to accept. The five are "
            f"{sorted(REQUIRED_DISCLOSURES)}."
        )
    missing = REQUIRED_DISCLOSURES - set(topics)
    if missing:
        raise DisclosuresNotAcknowledged(
            f"these disclosures were not acknowledged: {sorted(missing)}. Nothing is built "
            f"until every one of them has been shown and accepted."
        )
    return Acknowledgment(topics=frozenset(topics))


def require_acknowledgment(acknowledgment):
    """Refuse anything that is not a real acknowledgment.

    Require the acknowledgment type and the exact required topic set. A string,
    boolean or object with only similarly named fields does not pass.
    """
    if not isinstance(acknowledgment, Acknowledgment):
        raise DisclosuresNotAcknowledged(
            f"a deposit was built with {acknowledgment!r} in place of an acknowledgment. The "
            f"only way to obtain one is to acknowledge every required disclosure; a value "
            f"that merely looks like consent is not consent."
        )
    if set(acknowledgment.topics) != set(REQUIRED_DISCLOSURES):
        raise DisclosuresNotAcknowledged(
            f"the acknowledgment covers {sorted(acknowledgment.topics)}, which is not the "
            f"required set {sorted(REQUIRED_DISCLOSURES)}."
        )
    return acknowledgment
