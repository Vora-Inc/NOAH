"""Versioned ordinary onboarding. Frozen predecessor policies remain replayable."""

from dataclasses import replace

from .ledger import Q, State

MODE = "points-204040-v5"
PROFILE = "mainnet-production-points-204040-v5"
POLICY = "noah-points-204040-90d-no-pool/5"
V6_MODE = "points-204040-v6"
V6_PROFILE = "mainnet-production-points-204040-v6"
V6_POLICY = "noah-points-204040-90d-no-pool/6"
V6_SCHEMA = "noah-points-policy/6"
V6_RULE = "noah-deposit-zero-credit/1"
SWITCH_FIELD = "pointsNoahCreditSwitchDay"
ORDINARY_MODES = (MODE, V6_MODE)
V6_FIELDS = {
    "schema",
    "policy",
    "predecessor_json",
    "predecessor_sha256",
    "switch_day",
    "rule",
}
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


def v6_enabled(adapter):
    return getattr(adapter, "scoring_mode", None) == V6_MODE


def no_pool_mode(adapter):
    mode = adapter if isinstance(adapter, str) else getattr(adapter, "scoring_mode", None)
    return mode in ORDINARY_MODES


def validate_switch_day(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid NOAH credit switch day")
    return value


def initial_state():
    # Registration is day zero. First covered earning day starts the 90-day ramp.
    return replace(
        State(), baseline=20 * Q, maturity=Q // 100, points=Q // 5, last_compliant=Q // 5
    )


def manifest(pricing_manifest, *, mode=None, switch_day=None):
    selected = MODE if mode is None else mode
    if selected not in ORDINARY_MODES:
        raise ValueError("unsupported ordinary scoring mode")
    policy = V6_POLICY if selected == V6_MODE else POLICY
    if selected == V6_MODE:
        validate_switch_day(switch_day)
    from ..core.calculation_record import digest, encoded

    result = {**pricing_manifest, "policy": policy, "scoring_mode": selected, "rules": RULES}
    result["pricing_policy_sha256"] = pricing_manifest["sha256"]
    body = {"pricing": pricing_manifest, "rules": RULES, "policy": policy}
    if selected == V6_MODE:
        result["noah_credit_switch_day"] = switch_day
        result["rule"] = V6_RULE
        body["noah_credit_switch_day"] = switch_day
        body["rule"] = V6_RULE
    result["sha256"] = digest(encoded(body))
    return result


def validate_v6_document(document):
    """Hash-locked successor of the installed V5 policy. Preparation is not activation."""
    import hashlib
    import json

    from . import ema_policy
    from .amendment import _unique

    if type(document) is not dict or set(document) != V6_FIELDS:
        raise ValueError("unexpected V2.1 policy fields")
    if (document["schema"], document["policy"], document["rule"]) != (V6_SCHEMA, V6_POLICY, V6_RULE):
        raise ValueError("unsupported V2.1 policy identity")
    validate_switch_day(document["switch_day"])
    text = document["predecessor_json"]
    if (
        type(text) is not str
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != document["predecessor_sha256"]
    ):
        raise ValueError("V2.1 predecessor bytes differ")
    predecessor = json.loads(text, object_pairs_hook=_unique)
    if predecessor.get("schema") != ema_policy.SCHEMA:
        raise ValueError("V2.1 requires the installed V2 EMA policy as predecessor")
    ema_policy.validate_document(predecessor)
    cutoff = ema_policy.baseline(predecessor)["migration"]["cutoff_day"]
    if document["switch_day"] <= cutoff:
        raise ValueError("V2.1 switch day must follow the pinned legacy cutoff")
    return predecessor


def v6_predecessor(document):
    return validate_v6_document(document)


def build_v6_policy(predecessor_bytes, switch_day):
    """Build a held successor document. Writing it into a live runtime is a separate step."""
    import hashlib
    import json

    from .amendment import _unique

    if type(predecessor_bytes) is not bytes:
        raise ValueError("exact V2 policy bytes required")
    text = predecessor_bytes.decode("utf-8")
    json.loads(text, object_pairs_hook=_unique)
    document = dict(
        schema=V6_SCHEMA,
        policy=V6_POLICY,
        predecessor_json=text,
        predecessor_sha256=hashlib.sha256(predecessor_bytes).hexdigest(),
        switch_day=switch_day,
        rule=V6_RULE,
    )
    validate_v6_document(document)
    return document


def check_v6_transition(adapter, document):
    """Validate the first selection while the exact V2 policy is still installed."""
    import json
    import os
    from fnmatch import fnmatchcase

    from .state_files import directory, read

    validate_v6_document(document)
    root = adapter._submitted_record_dir
    pending = getattr(adapter, "_publish_intent", None)
    if pending is not None and pending.load() is not None:
        raise ValueError("pending publication forbids V2.1 switch")
    with directory(root) as scores:
        if read(scores, "points-policy.json") != document["predecessor_json"].encode("utf-8"):
            raise ValueError("first V2.1 selection requires the exact installed V2 policy")
        entries = set(os.listdir(scores))
        if entries.intersection(("starter-pending.json", "starter-intent.json")):
            raise ValueError("pending starter publication forbids V2.1 switch")
        for name in sorted(entries):
            if not fnmatchcase(name, "day-*.calculation.json"):
                continue
            try:
                record = json.loads(read(scores, name))
            except (OSError, TypeError, ValueError) as exc:
                raise ValueError("unsafe retained calculation") from exc
            day = record.get("day") if isinstance(record, dict) else None
            if type(day) is not int or day < 0 or name != f"day-{day}.calculation.json":
                raise ValueError("invalid retained calculation day")
            if day >= document["switch_day"]:
                # Successor calculations are valid only after the successor policy is durable.
                raise ValueError("a retained calculation already exists on or after the switch day")
    require_v2_cutover_day(adapter, document, root)
    return document


def require_v2_cutover_day(adapter, document, root):
    """The last V2 daily must already be finalized before selecting V2.1."""
    import json

    from ..core.burn_publication import confirmed_transition
    from ..core.calculation_record import CalculationEvidenceRefused
    from ..core.publish_intent import PublicationStatusUnknown
    from . import ema_policy
    from .mode import check_confirmed_daily, source_mode
    from .state_files import read_file

    switch = document["switch_day"]
    day = switch - 1
    chain = adapter.runner_chain
    latest = (chain.block_timestamp(chain.head_block()) - adapter.day_anchor_ts) // 86400 - 1
    if latest not in (day, switch):
        raise ValueError("V2.1 requires the planned last V2 closed day")
    try:
        daily = read_file(root, f"day-{day}.submitted.json")
        record = confirmed_transition(root, day) or json.loads(daily)
        reference = record["calculation_evidence"]
        mode, context, proof = source_mode(root, reference)
        predecessor = manifest(ema_policy.manifest(validate_v6_document(document), adapter.netuid))
        if mode != MODE or proof["day"] != day or context.get("points_policy") != predecessor:
            raise ValueError("last V2 calculation differs from predecessor")
        check_confirmed_daily(adapter, reference, proof, predecessor)
    except (OSError, KeyError, TypeError, ValueError, PublicationStatusUnknown, CalculationEvidenceRefused) as exc:
        raise ValueError("V2.1 requires the last pre-switch V2 daily confirmation") from exc


def excluded(raw):
    value = raw.get("pointsExcludedUids")
    if (
        not isinstance(value, list)
        or any(type(u) is not int or u < 0 for u in value)
        or value != sorted(set(value))
    ):
        raise ValueError("ordinary onboarding requires explicit owner-seat exclusions")
    return set(value)


def main():
    import argparse
    import hashlib
    import json
    from pathlib import Path

    from ..core.calculation_record import encoded

    parser = argparse.ArgumentParser(description="Prepare a held V2.1 NOAH credit policy")
    parser.add_argument("--predecessor", type=Path, required=True)
    parser.add_argument("--predecessor-sha256", required=True)
    parser.add_argument("--switch-day", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.predecessor.read_bytes()
    if hashlib.sha256(original).hexdigest() != args.predecessor_sha256:
        raise ValueError("predecessor digest differs")
    result = encoded(build_v6_policy(original, args.switch_day))
    if args.predecessor.read_bytes() != original:
        raise ValueError("predecessor changed")
    with args.output.open("xb") as stream:
        stream.write(result)
    print(json.dumps({"status": "prepared-only", "switch_day": args.switch_day,
                      "sha256": hashlib.sha256(result).hexdigest()}))


if __name__ == "__main__":
    main()
