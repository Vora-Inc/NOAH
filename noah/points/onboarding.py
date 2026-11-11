"""Versioned ordinary onboarding. Frozen predecessor policies remain replayable."""

from dataclasses import replace

from .ledger import Q, State

MODE = "points-204040-v5"
PROFILE = "mainnet-production-points-204040-v5"
POLICY = "noah-points-204040-90d-no-pool/5"
RULES = dict(
    base=20,
    contribution=40,
    target=50,
    bonusAt=75,
    maturityStart=1,
    maturityDays=90,
    maturityCurve=2,
    consistency=40,
    consistencyDays=30,
    consistencyCurve=2,
    debtKeep=50,
    debtZero=2,
)


def validate(value):
    if (
        not isinstance(value, dict)
        or value != RULES
        or any(type(v) is not int for v in value.values())
    ):
        raise ValueError("unsupported ordinary onboarding rules")
    return RULES


def enabled(adapter):
    return getattr(adapter, "scoring_mode", None) == MODE


def initial_state():
    # Registration is day zero. First covered earning day starts the 90-day ramp.
    return replace(
        State(), baseline=20 * Q, maturity=Q // 100, points=Q // 5, last_compliant=Q // 5
    )


def manifest(pricing_manifest):
    from ..core.calculation_record import digest, encoded

    result = {**pricing_manifest, "policy": POLICY, "scoring_mode": MODE, "rules": RULES}
    result["pricing_policy_sha256"] = pricing_manifest["sha256"]
    result["sha256"] = digest(
        encoded({"pricing": pricing_manifest, "rules": RULES, "policy": POLICY})
    )
    return result


def excluded(raw):
    value = raw.get("pointsExcludedUids")
    if (
        not isinstance(value, list)
        or any(type(u) is not int or u < 0 for u in value)
        or value != sorted(set(value))
    ):
        raise ValueError("ordinary onboarding requires explicit owner-seat exclusions")
    return set(value)
