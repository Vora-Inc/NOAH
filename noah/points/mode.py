"""Bind each new publication to the configured scoring mode and policy.

Reconciliation of an already sent transaction is deliberately separate: changing
configuration must not prevent recovery of its exact recorded bytes.
"""

from ..core.calculation_record import verify
from ..core.publish_intent import PublicationStatusUnknown
from . import amendment, ema_policy, onboarding
from .engine import INPUT_SCHEMA, POLICY

MODE = "points-402040-v2"


def source_mode(directory, reference):
    from ..core.calculation_record import artifact

    manifest = verify(directory, reference)
    fixture = artifact(directory, manifest, "fixture")
    context = artifact(directory, manifest, "context")
    score = artifact(directory, manifest, "agreed_score")
    mode = context.get("scoring_mode", "legacy")
    if mode == "legacy":
        if (
            fixture.get("schema") != "noah-fixture/6"
            or score.get("schema") != "noah-score-output/6"
            or context.get("points_policy") is not None
        ):
            raise PublicationStatusUnknown("legacy evidence contains another scoring mode")
    elif mode in (MODE, amendment.MODE, ema_policy.MODE, onboarding.MODE):
        expected_schema, expected_policy = (
            (ema_policy.INPUT_SCHEMA, ema_policy.POLICY)
            if mode in (ema_policy.MODE, onboarding.MODE)
            else (amendment.INPUT_SCHEMA, amendment.POLICY)
            if mode == amendment.MODE
            else (INPUT_SCHEMA, POLICY)
        )
        if mode == onboarding.MODE:
            expected_policy = onboarding.POLICY
            onboarding.validate(fixture.get("pointsRules"))
            onboarding.excluded(fixture)
        elif "pointsRules" in fixture or "pointsExcludedUids" in fixture:
            raise PublicationStatusUnknown("old mode contains new scoring rules")
        if (
            fixture.get("schema") != expected_schema
            or score.get("schema")
            != (
                ema_policy.OUTPUT_SCHEMA
                if mode in (ema_policy.MODE, onboarding.MODE)
                else "noah-points-output/2"
                if mode == amendment.MODE
                else "noah-points-output/1"
            )
            or score.get("policy") != expected_policy
            or not isinstance(context.get("points_policy"), dict)
            or context["points_policy"].get("policy") != expected_policy
        ):
            raise PublicationStatusUnknown("points evidence has inconsistent mode or policy")
    else:
        raise PublicationStatusUnknown("calculation uses an unsupported scoring mode")
    return mode, context, manifest


def check_publication_mode(adapter, reference):
    policy = getattr(adapter, "points_policy", None)
    mode = getattr(adapter, "scoring_mode", MODE if policy is not None else "legacy")
    selected, context, manifest = source_mode(adapter._submitted_record_dir, reference)
    if selected != mode:
        raise PublicationStatusUnknown("calculation scoring mode differs from configured mode")
    if mode in (MODE, amendment.MODE, ema_policy.MODE, onboarding.MODE):
        if policy is None or context.get("points_policy") != policy.manifest:
            raise PublicationStatusUnknown(
                "calculation points policy differs from configured policy"
            )
        if hasattr(policy, "fixture_migration"):
            from ..core.calculation_record import artifact

            fixture = artifact(adapter._submitted_record_dir, manifest, "fixture")
            from ..chain.sources import Refusal

            try:
                hotkeys = {int(k): v for k, v in context.get("scored_hotkeys", {}).items()}
                historical = {
                    k: v
                    for k, v in fixture.items()
                    if k
                    not in (
                        "pointsMigration",
                        "pointsReceipts",
                        "pointsHotkeys",
                        "pointsOwners",
                        "pointsPricing",
                        "pointsPricingBoundary",
                        "pointsRules",
                        "pointsExcludedUids",
                    )
                }
                if selected in (amendment.MODE, ema_policy.MODE, onboarding.MODE):
                    if fixture.get("pointsHotkeys") != {str(k): v for k, v in hotkeys.items()}:
                        raise ValueError("points fixture hotkeys differ from retained context")
                    if fixture.get("pointsOwners") != context.get("scored_coldkeys"):
                        raise ValueError("points fixture owners differ from retained context")
                    from .receipts import from_evidence, reconcile_fixture

                    if fixture.get("pointsReceipts") != from_evidence(
                        context.get("foreign_valuations", []),
                        ema=selected in (ema_policy.MODE, onboarding.MODE),
                    ):
                        raise ValueError(
                            "points receipt population differs from retained input context"
                        )
                    reconcile_fixture(fixture, fixture["pointsReceipts"])
                    if selected in (ema_policy.MODE, onboarding.MODE):
                        from .ema_reference import verify_dispositions

                        verify_dispositions(adapter._submitted_record_dir, fixture, policy)
                    else:
                        from .eligibility import verify_dispositions

                        verify_dispositions(
                            adapter._submitted_record_dir, fixture.get("pointsReceipts"), policy
                        )
                if selected == onboarding.MODE and fixture.get("pointsExcludedUids") != sorted(
                    adapter.owner_uids()
                ):
                    raise ValueError("owner-seat exclusions changed")
                policy.check_snapshot(adapter, historical, hotkeys)
                if fixture.get("pointsMigration") != policy.fixture_migration(fixture, hotkeys):
                    raise ValueError("migration seed differs")
            except (Refusal, ValueError, KeyError, TypeError) as exc:
                raise PublicationStatusUnknown(
                    "calculation migration history or seed differs from pinned policy"
                ) from exc
        if manifest["day"] <= policy.manifest["cutoff_day"]:
            raise PublicationStatusUnknown("points publication precedes activation cutoff")
    return selected
