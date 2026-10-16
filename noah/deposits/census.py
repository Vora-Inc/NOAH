"""The preflight that runs before anything is built, stated as a roll call.

the deposit preflight contract requires four things to be true every session and again before
every build: the Ark address matches the address pinned in Noah's own on-chain
identity record, the Ark's proxy record is empty, the deposit subnet's transfer
rail is on, and two independent reads of a settled block agree. Issue #4's AC-4
says the same from this tool's side, that the address is re-verified on every use
and a mismatch refuses to build anything.

**Why a roll call and not a list of failures.** The obvious shape is to collect
what went wrong and pass when nothing did. That shape has a property this project
has been bitten by twice: no observations produce no failures, which reads
exactly like a pass. It is what let a comparison gate print AGREE over two empty
lists (C-010) and a per-item check bless a recording that had dropped 1,500
blocks (C-012). Here the consequence is worse than a wrong number, because what
it clears the way for is a permanent, one-way transfer to an address nobody
checked. So every required check must be present and must have answered, and a
check nobody ran is refused by name rather than skipped past.

**Why results are checked by type and never by truth.** Every wire form of
"missing" is a non-empty string, and a non-empty string is true, so a result that
arrived undecoded as ``"unknown"`` would sail through a truthiness test. This is
written down in ``noah/chain/reads.py`` already, where the same discipline is
applied to storage answers for the same reason. Integers are refused too: a ``1``
that never went through a decoder is not a yes, and a census is the last place to
start guessing what a caller meant.

This module holds the verdict, not the reading. What performs the four reads is
the runner's chain plumbing, which hands its answers here to be judged.
"""

from dataclasses import dataclass

from ..chain.sources import Refusal

#: Every check that must have answered before anything is built. Named rather
#: than counted: a caller supplying four results, one of them misspelled, has
#: answered three questions and invented a fourth.
REQUIRED_CHECKS = frozenset(
    {
        # The Ark address equals the address pinned in Noah's on-chain subnet
        # identity record. Never a config file, a server, or DNS alone.
        "ark_address_matches_chain_pin",
        # The seal: the Ark's on-chain proxy record is empty, so no key can act
        # for it.
        "ark_proxy_record_empty",
        # The per-subnet transfer rail, read live. A switched-off rail is the
        # only deposit refusal that is nobody's mistake.
        "transfer_rail_enabled",
        # How many DISTINCT endpoints agreed about a settled block. A COUNT
        # rather than a yes, and the difference is the whole point: two RPC
        # connections to the SAME node agree trivially, so a boolean makes a
        # caller who did that honest and wrong. Ruled by NOAH-BUILD-LEAD as
        # exit-criteria T-L2: agreement theatre manufactures a pass, which is
        # worse than the gap it hides, and it is the cheapest-looking fix so it
        # is the one somebody reaches for.
        "sources_agree",
    }
)


#: The checks whose answer is a COUNT rather than a yes or no.
COUNTED_CHECKS = frozenset({"sources_agree"})

#: Distinct endpoints that must agree. Two connections to one node is one
#: endpoint, whatever the connection count says.
MINIMUM_DISTINCT_SOURCES = 2


class CensusIncomplete(Refusal):
    """A check was never run, or a name nobody recognises was answered instead."""


class CensusFailed(Refusal):
    """A check ran and did not pass, or answered with something that is not a yes or no."""


class ArkAddressUnconfirmed(Refusal):
    """The Ark address could not be confirmed against the chain's own pin."""


@dataclass(frozen=True)
class Census:
    """A preflight that answered every question and passed every one."""

    passed: bool


def census_verdict(observations):
    """Judge a completed preflight, or refuse.

    ``observations`` maps every name in :data:`REQUIRED_CHECKS` to the boolean
    the reader observed. Missing names, unknown names, and answers that are not
    real booleans are all refusals, and none of them is a failure to be collected
    and reported alongside the others: they mean the census did not happen.
    """
    if not isinstance(observations, dict):
        raise CensusIncomplete(
            f"a census verdict needs the observed results as a mapping of check name to "
            f"what was seen, got {observations!r}."
        )

    answered = set(observations)
    unknown = answered - REQUIRED_CHECKS
    if unknown:
        raise CensusIncomplete(
            f"the census answered {sorted(unknown)}, which this tool does not recognise "
            f"as a check. A misspelled name answers nothing while looking like diligence, "
            f"and the check it was meant to be is still missing."
        )

    missing = REQUIRED_CHECKS - answered
    if missing:
        raise CensusIncomplete(
            f"the census never ran {sorted(missing)}. This is refused rather than passed "
            f"over, because a verdict that collects failures finds none when nothing was "
            f"observed, and what clears here is a permanent one-way transfer."
        )

    for name in sorted(REQUIRED_CHECKS - COUNTED_CHECKS):
        seen = observations[name]
        if not isinstance(seen, bool):
            raise CensusFailed(
                f"the census check {name} answered {seen!r}, which is not a decoded yes or "
                f"no. Every wire form of 'missing' is a non-empty string and a non-empty "
                f"string is true, so this is checked by type and not by truth."
            )

    for name in sorted(COUNTED_CHECKS):
        seen = observations[name]
        if isinstance(seen, bool) or not isinstance(seen, int):
            raise CensusFailed(
                f"the census check {name} answered {seen!r}. This one is a COUNT of distinct "
                f"endpoints, not a yes or no, and a boolean is refused rather than read as "
                f"one: True would mean 1 under Python's own rules, which is exactly the value "
                f"this check exists to reject."
            )
        if seen < MINIMUM_DISTINCT_SOURCES:
            raise CensusFailed(
                f"only {seen} distinct endpoint(s) agreed, and {MINIMUM_DISTINCT_SOURCES} are "
                f"required. Two RPC connections to the SAME node are ONE endpoint and agree "
                f"trivially; counting them as two manufactures a pass, which is worse than "
                f"the gap it hides [exit-criteria T-L2]. A run that genuinely cannot reach a "
                f"second node records that it could not verify agreement and proceeds under "
                f"that recorded exception; it does not report a pass it did not earn, and "
                f"this function has no way to express one."
            )

    failed = sorted(name for name in REQUIRED_CHECKS - COUNTED_CHECKS if not observations[name])
    if failed:
        raise CensusFailed(
            f"the census failed on {failed}. Nothing is built, no flow is entered, and no "
            f"cached answer stands in: recovery is a clean re-read."
        )

    return Census(passed=True)


def confirm_ark_address(*, configured, chain_pin):
    """The Ark address, only once the chain's own pin agrees with what was configured.

    The configured value is never the authority and never the fallback. If the
    pin cannot be read, there is no address, because the entire purpose of the
    pin is that a config file alone cannot decide where a permanent, one-way
    transfer goes.
    """
    if not isinstance(chain_pin, str) or not chain_pin.strip():
        raise ArkAddressUnconfirmed(
            f"the Ark address pinned in the chain's own identity record could not be read "
            f"({chain_pin!r}), so there is no address. The configured value is not used as "
            f"a fallback here, ever: the pin exists precisely so that a config file, a "
            f"server, or DNS cannot decide where a one-way transfer goes."
        )
    if not isinstance(configured, str) or not configured.strip():
        raise ArkAddressUnconfirmed(
            f"no Ark address was configured to check against the pin ({configured!r})."
        )
    if configured != chain_pin:
        raise ArkAddressUnconfirmed(
            f"the configured Ark address {configured!r} is not the address pinned on chain "
            f"{chain_pin!r}. Nothing is built. One of the two is wrong and this tool cannot "
            f"tell which, so it declines to guess about the destination of a transfer that "
            f"can never be undone."
        )
    return chain_pin
