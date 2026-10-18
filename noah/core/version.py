"""Derive a deterministic publication key from the declared contract string.

Compare the resulting numeric key with the chain's configured version floor. This
is not a semantic-version comparison: a hash is not monotonic across contract
changes, and this key alone does not fingerprint all scoring implementation code.
Release review must verify that the contract label and configured floor are
appropriate for the intended deployment."""

import hashlib

from .settings import SettingsError

# Publication identity for the deployed surplus-bonus scoring rules. This is
# separate from the fixture wire schema (noah-fixture/6), which is unchanged. Bump this
# label when scoring rules change, then verify its numeric key against the chain
# floor; hash-derived keys are not ordered versions.
SCORING_CONTRACT = "noah-scoring/surplus-bonus-v1"


class ScoringVersionTooOld(SettingsError):
    """This validator's rules are older than the subnet requires."""


def version_key_for(contract):
    """A stable positive number derived from the scoring contract.

    The chain stores this as an unsigned 64-bit value, so the digest is trimmed
    to fit with room to spare rather than taken whole.
    """
    digest = hashlib.sha256(contract.encode()).hexdigest()[:12]
    return int(digest, 16)


OUR_VERSION_KEY = version_key_for(SCORING_CONTRACT)


def check_version_current(required):
    """Refuse when this validator's rules are older than the subnet demands."""
    try:
        floor = int(required)
    except (TypeError, ValueError):
        raise ScoringVersionTooOld(
            f"the subnet would not say which scoring version it requires; it answered "
            f"{required!r}. That is refused rather than assumed met, because assuming it "
            f"is met is exactly how a stale validator keeps running through the event "
            f"this check exists to catch."
        ) from None

    if OUR_VERSION_KEY < floor:
        raise ScoringVersionTooOld(
            f"this validator scores by version {OUR_VERSION_KEY} and the subnet requires "
            f"at least {floor}. Publishing anyway would put numbers on chain that no other "
            f"validator agrees with, and consensus would penalise this one for the "
            f"disagreement without anything ever reporting an error. Upgrade this "
            f"validator to the current scoring rules and restart it."
        )
    return None
