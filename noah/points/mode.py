"""Bind each new publication to the configured scoring mode and policy.

Reconciliation of an already sent transaction is deliberately separate: changing
configuration must not prevent recovery of its exact recorded bytes.
"""

import json

from ..core.calculation_record import verify
from ..core.publish_intent import PublicationStatusUnknown
from . import amendment, ema_policy, onboarding
from .engine import INPUT_SCHEMA, POLICY

MODE = "points-402040-v2"
_ORDINARY = (onboarding.MODE, onboarding.V6_MODE)


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
    elif mode in (MODE, amendment.MODE, ema_policy.MODE, *_ORDINARY):
        expected_schema, expected_policy = (
            (ema_policy.INPUT_SCHEMA, ema_policy.POLICY)
            if mode in (ema_policy.MODE, *_ORDINARY)
            else (amendment.INPUT_SCHEMA, amendment.POLICY)
            if mode == amendment.MODE
            else (INPUT_SCHEMA, POLICY)
        )
        if mode in _ORDINARY:
            expected_policy = onboarding.V6_POLICY if mode == onboarding.V6_MODE else onboarding.POLICY
            onboarding.validate(fixture.get("pointsRules"))
            onboarding.excluded(fixture)
            if mode == onboarding.V6_MODE:
                pinned = context.get("points_policy")
                if (
                    not isinstance(pinned, dict)
                    or type(fixture.get(onboarding.SWITCH_FIELD)) is not int
                    or not 0 <= fixture[onboarding.SWITCH_FIELD] <= manifest["day"]
                    or fixture[onboarding.SWITCH_FIELD] != pinned.get("noah_credit_switch_day")
                ):
                    raise PublicationStatusUnknown(
                        "NOAH credit switch day differs from the pinned policy"
                    )
            elif onboarding.SWITCH_FIELD in fixture:
                raise PublicationStatusUnknown("V2 evidence contains a V2.1 switch day")
        elif "pointsRules" in fixture or "pointsExcludedUids" in fixture or onboarding.SWITCH_FIELD in fixture:
            raise PublicationStatusUnknown("old mode contains new scoring rules")
        if (
            fixture.get("schema") != expected_schema
            or score.get("schema")
            != (
                ema_policy.OUTPUT_SCHEMA
                if mode in (ema_policy.MODE, *_ORDINARY)
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


def check_historical_refresh(adapter, reference, manifest, refresh, predecessor):
    """Only a previously confirmed canonical daily vector may cross the switch."""
    day = manifest["day"]
    if (type(refresh) is not dict or set(refresh) != {"day", "height", "epoch"}
            or any(type(refresh[k]) is not int for k in refresh)
            or refresh["day"] != day or refresh["height"] != manifest["height"]
            or refresh["epoch"] <= 0):
        raise PublicationStatusUnknown("pre-switch calculation is refresh-only")
    check_confirmed_daily(adapter, reference, manifest, predecessor)


def check_confirmed_daily(adapter, reference, manifest, predecessor):
    from ..core.burn_publication import confirmed_transition
    from ..core.direct import reconcile, same_address
    from ..core.submitted_record import SCHEMA
    from ..core.version import version_key_for
    from .state_files import read_file

    day = manifest["day"]
    root = adapter._submitted_record_dir
    try:
        if type(day) is not int or day < 0:
            raise ValueError("invalid receipt day")
        daily = json.loads(read_file(root, f"day-{day}.submitted.json", required=True))
        record = confirmed_transition(root, day) or daily
        marker = record["direct_intent"]
        result = marker["result"]
        if (record["schema"] != SCHEMA or record["publish_mode"] != "direct"
                or record["day"] != marker["day"] or marker["day"] != day
                or record["height"] != marker["height"] or marker["height"] != manifest["height"]
                or record["netuid"] != adapter.netuid or marker["netuid"] != adapter.netuid
                or record["calculation_evidence"] != reference or marker["calculation_evidence"] != reference
                or record["submitted_weights_u16"] != marker["vector"]
                or marker["vector"] != manifest["publishable_weights_u16"]
                or marker.get("refresh_epoch") is not None
                or marker["version_key"] != version_key_for(predecessor["policy"] + "/" + predecessor["sha256"])
                or type(result["block"]) is not int or type(result["extrinsic_index"]) is not int
                or result["block"] != record["block"] or result["block_hash"] != record["block_hash"]
                or result["extrinsic_index"] != record["extrinsic_index"]):
            raise ValueError("receipt differs")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PublicationStatusUnknown("no exact confirmed pre-switch daily receipt") from exc
    check_identity = getattr(adapter, "_check_direct_identity", None)
    if check_identity is None:
        # The Ark designation belongs to the scoring policy. A deployment
        # can publish from a different explicitly configured validator seat.
        validator = getattr(adapter, "expected_signer_address", None) or adapter.designated_validator_hotkey
        proxy = getattr(adapter, "proxy_signer_address", None)
        route = "proxy" if proxy else "direct"
        if (marker.get("mode") != "direct" or marker.get("mecid") != 0
                or marker.get("route") != route
                or not same_address(marker.get("hotkey"), validator)
                or not same_address(marker.get("signer"), proxy or validator)):
            raise PublicationStatusUnknown("last V2 publisher differs from configured identity")
        from .transition import confirm_paired_receipt

        confirm_paired_receipt(adapter.runner_chain, marker)
    else:
        check_identity(marker)
        reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)


def check_publication_mode(adapter, reference, *, refresh=None):
    return _check_calculation_mode(adapter, reference, refresh=refresh)


def check_accounting_mode(adapter, reference):
    """Prove the calculation for an explicitly non-publishing catch-up record."""
    return _check_calculation_mode(adapter, reference, accounting=True)


def _check_calculation_mode(adapter, reference, *, refresh=None, accounting=False):
    policy = getattr(adapter, "points_policy", None)
    mode = getattr(adapter, "scoring_mode", MODE if policy is not None else "legacy")
    selected, context, manifest = source_mode(adapter._submitted_record_dir, reference)
    expected = getattr(policy, "manifest", None)
    if mode == onboarding.V6_MODE and policy is not None:
        expected = policy.manifest_for_day(manifest["day"])
        mode = expected["scoring_mode"]
        if selected == mode == onboarding.MODE and not accounting:
            check_historical_refresh(adapter, reference, manifest, refresh, expected)
    if selected != mode:
        raise PublicationStatusUnknown("calculation scoring mode differs from configured mode")
    if mode in (MODE, amendment.MODE, ema_policy.MODE, *_ORDINARY):
        if policy is None or context.get("points_policy") != expected:
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
                        onboarding.SWITCH_FIELD,
                    )
                }
                if selected in (amendment.MODE, ema_policy.MODE, *_ORDINARY):
                    if fixture.get("pointsHotkeys") != {str(k): v for k, v in hotkeys.items()}:
                        raise ValueError("points fixture hotkeys differ from retained context")
                    if fixture.get("pointsOwners") != context.get("scored_coldkeys"):
                        raise ValueError("points fixture owners differ from retained context")
                    from .receipts import from_evidence, reconcile_fixture

                    if fixture.get("pointsReceipts") != from_evidence(
                        context.get("foreign_valuations", []),
                        ema=selected in (ema_policy.MODE, *_ORDINARY),
                    ):
                        raise ValueError(
                            "points receipt population differs from retained input context"
                        )
                    reconcile_fixture(fixture, fixture["pointsReceipts"])
                    if selected in (ema_policy.MODE, *_ORDINARY):
                        from .ema_reference import verify_dispositions

                        verify_dispositions(adapter._submitted_record_dir, fixture, policy)
                    else:
                        from .eligibility import verify_dispositions

                        verify_dispositions(
                            adapter._submitted_record_dir, fixture.get("pointsReceipts"), policy
                        )
                if selected in _ORDINARY and fixture.get("pointsExcludedUids") != sorted(
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
