"""Opt-in report enforcement at allocation and direct-publication boundaries."""

import json
import os
from pathlib import Path
import stat
import tempfile

from ..chain.ss58 import public_key_of
from .calculation_record import artifact, durable_write, encoded, verify
from .report_publication import (
    ReportPublicationRefused, allocation_inputs, replay_package, require,
    retain_report, run_worker, semantic_digest, validate_result, verify_report,
)

ELIGIBILITY = "registered-at-b-accurate-report-v1"


def enabled(adapter):
    return getattr(adapter, "report_gate_enabled", False) is True


def load_policy(path):
    from ..reports.adjudication_contract import MAX_REQUEST_BYTES, strict_json
    try:
        with Path(path).open("rb") as handle:
            policy = strict_json(handle.read(MAX_REQUEST_BYTES + 1))
        return validate_policy(policy)
    except (OSError, ValueError) as exc:
        raise ReportPublicationRefused("report configuration is missing or invalid") from exc


def validate_policy(policy):
    """Pure frozen-policy validation, also used by recovery configuration checks."""
    if type(policy) is dict and policy.get("schema") == "noah.report-schedule/1":
        from ..reports.schedule import validate_schedule
        validate_schedule(policy)
        return policy
    if type(policy) is dict and policy.get("schema") == "noah.weekly-assignment-template/2":
        from ..reports.assignments import validate_template
        validate_template(policy)
        return policy
    from ..reports.verification import validate_round
    require(type(policy) is dict and set(policy) == {
        "schema", "effective_day", "genesis_hash", "mining_netuid",
        "eligibility_policy_version", "required_rounds"}, "report policy fields differ")
    require(policy["schema"] == "noah.report-policy/1"
            and policy["eligibility_policy_version"] == ELIGIBILITY
            and type(policy["effective_day"]) is int and policy["effective_day"] >= 0,
            "unsupported report policy")
    rounds = policy["required_rounds"]
    require(type(rounds) is list and 0 < len(rounds) <= 256, "report schedule is empty or excessive")
    for contract in rounds:
        validate_round(contract)
        require(contract["eligibility_policy_version"] == ELIGIBILITY
                and contract["genesis_hash"] == policy["genesis_hash"]
                and contract["mining_netuid"] == policy["mining_netuid"],
                "report round and policy differ")
    require(len({semantic_digest(row) for row in rounds}) == len(rounds), "duplicate report rounds")
    return policy


def resolve_schedule(configuration, endpoints, day):
    """Private IPC keeps potentially credentialed endpoints out of process argv."""
    with tempfile.TemporaryDirectory(prefix="noah-report-schedule-") as temporary:
        path = Path(temporary) / "invocation.json"
        path.write_bytes(encoded(dict(schedule=configuration, day=day,
                                      endpoints=endpoints, require_pair=True)))
        path.chmod(0o600)
        return run_worker("resolve-day", "--invocation", path)


def select_policy(configuration, endpoints, day, anchor):
    resolution = None
    if configuration["schema"] == "noah.report-schedule/1":
        require(configuration["day_anchor_ts"] == anchor, "report schedule calendar differs")
        resolution = resolve_schedule(configuration, endpoints, day)
        from ..reports.schedule import verify_resolution
        verify_resolution(configuration, day, resolution, require_pair=True)
        policy = resolution["policy"]
    else:
        policy = policy_for_day(configuration, day, anchor)
    selection = dict(schema="noah-report-selection/1", configuration=configuration,
                     configuration_sha256=semantic_digest(configuration), resolution=resolution)
    return policy, selection


def verify_selection(selection, request):
    require(type(selection) is dict and set(selection) == {
        "schema", "configuration", "configuration_sha256", "resolution"}
            and selection["schema"] == "noah-report-selection/1", "invalid report selection proof")
    configuration = validate_policy(selection["configuration"])
    require(semantic_digest(configuration) == selection["configuration_sha256"],
            "report selection configuration digest differs")
    if configuration["schema"] == "noah.report-schedule/1":
        from ..reports.schedule import verify_resolution
        require(configuration["day_anchor_ts"] == request["day_anchor_ts"],
                "frozen report schedule calendar differs")
        verify_resolution(configuration, request["eval_day"], selection["resolution"], require_pair=True)
        policy = selection["resolution"]["policy"]
    elif configuration["schema"] == "noah.weekly-assignment-template/2":
        policy = _verify_weekly_selection(configuration, selection["resolution"], request)
    else:
        require(selection["resolution"] is None, "explicit report policy has schedule evidence")
        policy = policy_for_day(configuration, request["eval_day"], request["day_anchor_ts"])
    require(policy == request["policy"] and semantic_digest(policy) == request["policy_sha256"],
            "frozen report selection does not reproduce policy")


def policy_for_day(policy, day, anchor):
    from ..reports.gate import eval_day_for_close
    require(day >= policy["effective_day"], "report policy is not effective for this day")
    rounds = [row for row in policy["required_rounds"]
              if eval_day_for_close(row["reveal_close_ms"], anchor) == day]
    require(rounds, "report schedule has no required round for the scoring day")
    return {**policy, "required_rounds": rounds}


def configure(adapter, settings):
    adapter.report_gate_enabled = False
    adapter.weekly_assignments = False
    if not getattr(settings, "report_gate_enabled", False):
        return
    require(settings.publish_mode == "direct", "report enforcement requires direct publication")
    require(adapter._submitted_record_dir is not None and adapter._publish_intent is not None,
            "report enforcement requires durable calculation and intent storage")
    policy = load_policy(settings.report_gate_policy)
    weekly = getattr(settings, "weekly_assignments", False) is True
    require((policy.get("schema") == "noah.weekly-assignment-template/2") == weekly,
            "weekly assignments require NOAH_WEEKLY_ASSIGNMENTS and the weekly template")
    require(policy["genesis_hash"] == adapter.runner_chain.genesis_hash().removeprefix("0x")
            and policy["mining_netuid"] == adapter.netuid, "report policy network differs")
    adapter.report_reward = getattr(settings, "report_reward", "gate")
    require(adapter.report_reward in ("gate", "bonus-20"), "unsupported report reward")
    adapter.report_gate_policy_path = settings.report_gate_policy
    bind_activation(adapter, policy, persist=False)
    adapter.report_gate_enabled = True
    adapter.weekly_assignments = weekly
    adapter.weekly_assignment_signing_key_file = getattr(settings, "weekly_assignment_signing_key_file", None)
    if weekly:
        require(settings.scoring_mode == "points-204040-v5" and adapter.report_reward == "bonus-20",
                "weekly assignments require V2 bonus-20")
        from .weekly_runtime import bind_template, signing_pair
        bind_template(adapter._submitted_record_dir, policy)
        if adapter.weekly_assignment_signing_key_file:
            signing_pair(adapter.weekly_assignment_signing_key_file, policy["assignment_signer_public_key"])


def publish_weekly(adapter):
    """One tick of the validator loop. Off unless weekly assignments are configured.

    The finalized chain clock selects the UTC week. No date is supplied. A
    valid file is not rewritten. The first tick after a restart catches up
    when Sunday's file is missing. Failures wait five minutes and do not
    stop the scoring loop.
    """
    if getattr(adapter, "weekly_assignments", False) is not True or not enabled(adapter):
        return None
    import time
    now = time.monotonic()
    if now < getattr(adapter, "_weekly_publish_not_before", 0):
        return getattr(adapter, "_weekly_publish_last", None)
    key = getattr(adapter, "weekly_assignment_signing_key_file", None)
    if not key:
        return None  # Read-only follower; never substitute a transaction signer.
    from ..chain.sources import Refusal
    try:
        from ..reports.assignments import (validate_unsigned_week, template_from_document,
                                           week_start_date, sign_week, retain_week)
        from .weekly_runtime import bind_template, signing_pair
        configuration = load_policy(adapter.report_gate_policy_path)
        bind_activation(adapter, configuration, persist=True)
        bind_template(adapter._submitted_record_dir, configuration, persist=True)
        endpoints = list(adapter.runner_chain.endpoints.values())
        require(endpoints, "weekly publication needs a chain endpoint")
        invocation = {"schema": "noah.weekly-assignment-build/1", "template": configuration,
                      "endpoint": endpoints[0]}
        with tempfile.TemporaryDirectory(prefix="noah-weekly-build-") as directory:
            path = Path(directory) / "invocation.json"
            path.write_bytes(encoded(invocation))
            path.chmod(0o600)
            result = run_worker("assignment-week", "--invocation", path)
        require(type(result) is dict and set(result) == {"schema", "week", "finalized"}
                and result["schema"] == "noah.weekly-assignment-build-result/1", "invalid weekly builder result")
        body, finalized = result["week"], result["finalized"]
        validate_unsigned_week(body, trusted_public_key=configuration["assignment_signer_public_key"])
        require(template_from_document(body) == template_from_document(configuration), "weekly builder template differs")
        chain = adapter.runner_chain
        current_head = chain.head_block()
        current_timestamp_ms = chain.block_timestamp_ms(current_head)
        require(type(finalized) is dict and set(finalized) == {"block_number", "block_hash", "timestamp_ms"}
                and type(finalized["block_number"]) is int and finalized["block_number"] >= 0
                and current_head >= finalized["block_number"]
                and chain.block_hash(finalized["block_number"]).removeprefix("0x") == finalized["block_hash"]
                and chain.block_timestamp_ms(finalized["block_number"]) == finalized["timestamp_ms"]
                and type(finalized["timestamp_ms"]) is int
                and 0 <= current_timestamp_ms - finalized["timestamp_ms"] <= 300_000
                and body["week_start"] == week_start_date(finalized["timestamp_ms"] // 1000).isoformat()
                and body["week_start"] == week_start_date(current_timestamp_ms // 1000).isoformat(),
                "weekly builder finalized identity differs")
        signed = sign_week(body, signing_pair(key, configuration["assignment_signer_public_key"]),
                           trusted_public_key=configuration["assignment_signer_public_key"])
        result = retain_week(Path(adapter._submitted_record_dir) / "report-state/weekly-assignments", signed,
                             trusted_public_key=configuration["assignment_signer_public_key"])
    except (Exception, Refusal) as error:
        from ..diagnostics import write as diagnostic
        adapter._weekly_publish_not_before = now + 300
        diagnostic(f"[weekly] assignment file not written ({type(error).__name__})", severity="error")
        return None
    adapter._weekly_publish_last = result
    adapter._weekly_publish_not_before = now + 300
    return result


def startup_binding(settings, genesis, directory):
    """Reuse normal configuration checks before wallet load, without writes."""
    if not getattr(settings, "report_gate_enabled", False):
        return
    from types import SimpleNamespace
    adapter = SimpleNamespace(
        netuid=settings.netuid, _submitted_record_dir=directory,
        _publish_intent=object(),
        runner_chain=SimpleNamespace(genesis_hash=lambda: genesis),
    )
    configure(adapter, settings)  # bind_activation(persist=False); no collection.


def bind_activation(adapter, configuration, *, persist):
    """Freeze only network/cutoff; never silently move activation on restart."""
    activation = dict(schema="noah-report-activation/1", genesis_hash=configuration["genesis_hash"],
                      mining_netuid=configuration["mining_netuid"], effective_day=configuration["effective_day"])
    if getattr(adapter, "report_reward", "gate") != "gate":
        activation["reward"] = adapter.report_reward
    validate_activation(activation)
    require(activation["genesis_hash"] == adapter.runner_chain.genesis_hash().removeprefix("0x")
            and activation["mining_netuid"] == adapter.netuid, "report activation network differs")
    from .report_reward_transition import read_document, configured_transition, digest
    path = Path(adapter._submitted_record_dir) / "report-state" / "activation.json"
    require(not path.is_symlink() and not path.parent.is_symlink(), "report activation link refused")
    if path.exists():
        retained = validate_activation(read_document(path))
        require(all(retained[k] == activation[k] for k in
                    ("genesis_hash", "mining_netuid", "effective_day")),
                "retained report activation cutoff differs")
        activation = retained
    else:
        require(not any(path.parent.glob("day-*.request.json"))
                and not any(path.parent.glob("day-*.no-bonus.json"))
                and not (path.parent / "weekly-template.json").exists()
                and not (path.parent / "weekly-template.json").is_symlink(),
                "report request or weekly evidence exists without its activation boundary")
        reports = Path(adapter._submitted_record_dir) / "reports"
        require(not reports.exists() and not reports.is_symlink(),
                "report evidence exists without its activation boundary")
    transition = configured_transition(adapter._submitted_record_dir, activation,
                                       getattr(adapter, "report_reward", "gate"))
    if transition is not None and hasattr(adapter, "day_anchor_ts"):
        require(adapter.day_anchor_ts == transition["day_anchor_ts"], "report transition calendar differs")
    binding = None if transition is None else digest(transition)
    require(getattr(adapter, "_report_transition_digest", binding) == binding,
            "report reward transition changed during execution")
    require(getattr(adapter, "_report_activation", activation) == activation,
            "report activation cutoff changed during execution")
    if persist and not path.exists():
        durable_write(path, encoded(activation))
    adapter._report_transition_digest = binding
    adapter._report_reward_transition = transition
    adapter._report_activation = activation
    return activation


def validate_activation(value):
    from .report_publication import SHA
    require(type(value) is dict and set(value) == ({
        "schema", "genesis_hash", "mining_netuid", "effective_day"}
        | ({"reward"} if "reward" in value else set()))
            and value.get("reward", "gate") in ("gate", "bonus-20")
            and value["schema"] == "noah-report-activation/1", "invalid report activation schema")
    require(type(value["genesis_hash"]) is str and SHA.fullmatch(value["genesis_hash"])
            and type(value["mining_netuid"]) is int and 1 <= value["mining_netuid"] <= 65535
            and type(value["effective_day"]) is int and 0 <= value["effective_day"] < 2**32,
            "invalid report activation identity")
    return value


def selected_reward(adapter, day):
    from .report_reward_transition import reward_for_day
    return reward_for_day(adapter._report_activation, adapter._report_reward_transition, day)


def transition_reference(adapter, day):
    from .report_reward_transition import reference_for
    return reference_for(adapter._report_reward_transition, day)


def _verify_weekly_selection(configuration, resolution, request):
    from ..reports.assignments import schedule_for_target, template_from_document
    from ..reports.schedule import verify_resolution
    require(type(resolution) is dict, "invalid weekly resolution")
    compact = resolution.get("schema") == "noah.weekly-resolution/2"
    require((compact and set(resolution) == {"schema", "week", "base"}) or
            (resolution.get("schema") == "noah.weekly-resolution/1" and
             set(resolution) == {"schema", "week", "rounds"}), "invalid weekly resolution")
    from .weekly_runtime import verify_week_for
    week = verify_week_for(configuration, resolution["week"], request["eval_day"], request["day_anchor_ts"])
    require(template_from_document(week) == template_from_document(configuration),
            "weekly template differs from the signed file")
    needed = sorted({netuid for row in week["assignments"] for netuid in row["targets"]})
    if compact:
        from .weekly_runtime import expand_resolution
        rounds = expand_resolution(configuration, week, request["eval_day"], request["day_anchor_ts"],
                                   resolution["base"])
    else:
        rounds = resolution["rounds"]
    require(type(rounds) is list and len(rounds) == len(needed), "weekly round count differs")
    template = template_from_document(configuration)
    collected = []
    for netuid, item in zip(needed, rounds):
        schedule = schedule_for_target(template, netuid)
        verify_resolution(schedule, request["eval_day"], item, require_pair=True)
        collected.append(item["round"])
    policy = _weekly_policy(configuration, collected)
    require(request.get("assignments") == week["assignments"], "frozen assignments differ")
    return policy


def _weekly_policy(configuration, rounds):
    return {"schema": "noah.report-policy/1", "effective_day": configuration["effective_day"],
            "genesis_hash": configuration["genesis_hash"], "mining_netuid": configuration["mining_netuid"],
            "eligibility_policy_version": configuration["eligibility_policy_version"],
            "required_rounds": rounds}


def _retained_week(root, day, anchor, configuration):
    from ..reports.assignments import week_start_date
    week = week_start_date(anchor + day * 86400).isoformat()
    path = Path(root) / "report-state" / "weekly-assignments" / f"{week}.json"
    if not path.is_file():
        return None
    from .weekly_runtime import verify_week_for
    require(not path.is_symlink() and not path.parent.is_symlink(), "weekly file link refused")
    from ..reports.adjudication_contract import strict_json, MAX_REQUEST_BYTES
    with path.open("rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    require(len(data) <= MAX_REQUEST_BYTES, "weekly file exceeds byte limit")
    return verify_week_for(configuration, strict_json(data), day, anchor)


def select_weekly(configuration, week, endpoints, day, anchor):
    from ..reports.assignments import schedule_for_target, template_from_document
    from .weekly_runtime import verify_week_for
    week = verify_week_for(configuration, week, day, anchor)
    require(template_from_document(week) == template_from_document(configuration),
            "weekly template differs from the signed file")
    needed = sorted({netuid for row in week["assignments"] for netuid in row["targets"]})
    require(needed, "weekly file has no assigned subnet")
    template = template_from_document(configuration)
    from .weekly_runtime import expand_resolution
    schedule = schedule_for_target(template, needed[0])
    require(schedule["day_anchor_ts"] == anchor, "weekly calendar differs")
    base = resolve_schedule(schedule, endpoints, day)
    resolutions = expand_resolution(configuration, week, day, anchor, base)
    policy = _weekly_policy(configuration, [item["round"] for item in resolutions])
    selection = {"schema": "noah-report-selection/1", "configuration": configuration,
                 "configuration_sha256": semantic_digest(configuration),
                 "resolution": {"schema": "noah.weekly-resolution/2", "week": week, "base": base}}
    return policy, selection


def configuration_for_day(adapter, configuration, day):
    transition = getattr(adapter, "_report_reward_transition", None)
    if configuration.get("schema") == "noah.weekly-assignment-template/2" and transition is not None and day < transition["effective_day"]:
        return validate_policy(transition["announcement"]["configuration"])
    return configuration


def request_for(adapter, reference, policy, assignments=None):
    from ..reports.adjudication_contract import validate_request
    root = adapter._submitted_record_dir
    manifest = verify(root, reference)
    context = artifact(root, manifest, "context")
    fixture = artifact(root, manifest, "fixture")
    require("report_gate" not in context and "starter_overlay" not in context,
            "report request needs raw daily evidence")
    chain = adapter.runner_chain
    observed = context["to_block"]
    evaluation = chain.head_block()
    require(evaluation >= observed, "report evaluation precedes scored identities")
    hotkeys = chain.seat_hotkeys(observed)
    registrations = chain.registration_blocks(observed)
    owners = chain.hotkey_owners(list(hotkeys.values()), observed)
    seats = []
    for row in sorted((r for r in fixture["miners"] if r.get("uid") is not None),
                      key=lambda r: r["uid"]):
        uid = row["uid"]
        hot = context["scored_hotkeys"].get(str(uid))
        require(hot is not None and hotkeys.get(uid) == hot
                and owners.get(hot) == row["coldkey"], "report scored identity differs")
        registration = registrations.get(uid)
        require(type(registration) is int and 0 < registration <= observed,
                "report scored registration is missing")
        seats.append(dict(uid=uid, hotkey=public_key_of(hot).hex(),
                          coldkey=public_key_of(row["coldkey"]).hex(),
                          registration_block=registration,
                          registration_day=(chain.block_timestamp(registration) - adapter.day_anchor_ts) // 86400,
                          observed_at={"block_number": observed,
                                       "block_hash": chain.block_hash(observed).removeprefix("0x")}))
    request = dict(schema="noah.report-adjudication-request/1", policy=policy,
                   policy_sha256=semantic_digest(policy),
                   genesis_hash=manifest["genesis_hash"].removeprefix("0x"),
                   mining_netuid=adapter.netuid, eval_day=manifest["day"],
                   day_anchor_ts=adapter.day_anchor_ts,
                   evaluation={"block_number": evaluation,
                               "block_hash": chain.block_hash(evaluation).removeprefix("0x"),
                               "timestamp_ms": chain.block_timestamp_ms(evaluation)},
                   calculation={"reference": reference,
                                "fixture_sha256": manifest["artifacts"]["fixture"]["sha256"]},
                   scored_seats=seats)
    if assignments is not None:
        request["assignments"] = assignments
    validate_request(request)
    return request


def resume_raw_source(adapter, current, frozen):
    """Resume a frozen pre-send request without changing its financial evidence.

    A later loop pin changes only the attempt height in an otherwise identical
    raw calculation. Keep that new attempt's audit record, but use the original
    R0 for its existing request/marker. Any other difference still refuses.
    """
    root = adapter._submitted_record_dir
    latest = verify(root, current)
    intent = getattr(adapter, "_publish_intent", None)
    require(intent is None or intent.load() is None,
            "frozen report retry has an unresolved publication intent")
    original = verify(root, frozen)
    contexts = [artifact(root, manifest, "context") for manifest in (latest, original)]
    for reference, manifest, context in zip((current, frozen), (latest, original), contexts):
        require(reference == {
            "path": reference["path"], "sha256": reference["sha256"],
            "day": manifest["day"], "genesis_hash": manifest["genesis_hash"],
            "candidate_sha256": context.get("candidate", {}).get("source_sha256"),
        } and context["height"] == manifest["height"]
            and not any(name in context for name in
                        ("report_gate", "report_no_bonus", "starter_overlay", "burn_allocation")),
                "frozen report retry requires authentic raw calculation references")
    require(type(original["height"]) is int and type(latest["height"]) is int
            and original["height"] <= latest["height"], "frozen report retry moves backwards")
    require({k: v for k, v in contexts[0].items() if k != "height"}
            == {k: v for k, v in contexts[1].items() if k != "height"},
            "frozen report retry calculation context differs")
    expected = {**latest, "height": original["height"],
                "artifacts": {**latest["artifacts"], "context": original["artifacts"]["context"]}}
    require(original == expected, "frozen report retry financial evidence or vector differs")
    return frozen, original


def prepare_daily(adapter, vector):
    if not enabled(adapter):
        return vector
    root = Path(adapter._submitted_record_dir)
    source = adapter._calculation_reference
    require(source is not None, "report enforcement needs raw calculation evidence")
    manifest = verify(root, source)
    attempt_height = manifest["height"]
    configuration = load_policy(adapter.report_gate_policy_path)
    bind_activation(adapter, configuration, persist=True)
    if manifest["day"] < configuration["effective_day"]:
        require("report_gate" not in artifact(root, manifest, "context"),
                "pre-effective source unexpectedly contains report proof")
        return vector
    configuration = configuration_for_day(adapter, configuration, manifest["day"])
    endpoints = list(adapter.runner_chain.endpoints.values())
    assignments = None
    if configuration.get("schema") == "noah.weekly-assignment-template/2":
        from .weekly_runtime import bind_template, marker_path, validate_no_bonus_marker, retain_no_bonus
        from .report_reward_transition import read_document
        bind_template(root, configuration, persist=True)
        marker = marker_path(root, manifest["day"])
        if marker.exists() or marker.is_symlink():
            saved = read_document(marker)
            validate_no_bonus_marker(root, saved)
            source, manifest = resume_raw_source(adapter, source, saved["source"])
            require({str(k): v for k, v in vector.items()} == manifest["publishable_weights_u16"],
                    "frozen no-bonus source or vector differs; preserve original attempt")
            adapter._calculation_reference = saved["reference"]
            return vector
        try:
            week = _retained_week(root, manifest["day"], adapter.day_anchor_ts, configuration)
        except (OSError, ValueError, ReportPublicationRefused):
            week = None  # Invalid/unavailable infrastructure can never earn a bonus.
        if week is None or not any(row["targets"] for row in week["assignments"]):
            require({str(k): v for k, v in vector.items()} == manifest["publishable_weights_u16"], "no-bonus raw vector differs")
            adapter._calculation_reference = retain_no_bonus(root, source, configuration,
                reason="assignment_unavailable" if week is None else "no_assigned_targets", week=week,
                transition=transition_reference(adapter, manifest["day"]))
            return vector
        policy, selection = select_weekly(configuration, week, endpoints, manifest["day"], adapter.day_anchor_ts)
        assignments = week["assignments"]
    else:
        policy, selection = select_policy(configuration, endpoints, manifest["day"], adapter.day_anchor_ts)
    # Freeze the first evaluation and policy before any provider collection.
    # Restart/retry never silently retimes a signed round or substitutes a policy.
    path = root / "report-state" / f"day-{manifest['day']}.request.json"
    if path.exists():
        request = json.loads(path.read_text())
        source, manifest = resume_raw_source(adapter, source, request["calculation"]["reference"])
        require(request["calculation"]["fixture_sha256"] == manifest["artifacts"]["fixture"]["sha256"]
                and request["policy"] == policy
                and request.get("assignments") == assignments,
                "frozen report request differs; preserve original attempt")
    else:
        request = request_for(adapter, source, policy, assignments)
        durable_write(path, encoded(request))
    from ..reports.adjudication_contract import validate_invocation
    require(len(endpoints) == 2, "report collection requires two independent readers")
    invocation = {"schema": "noah.report-adjudication-invocation/1", "request": request,
                  "sources": [{"id": name, "endpoint": endpoint, "history": None}
                              for name, endpoint in zip(("reader_a", "reader_b"), endpoints)]}
    validate_invocation(invocation)
    packages = root / "reports"
    packages.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".report-collection-", dir=root) as temporary:
        scratch = Path(temporary)
        invoke = scratch / "invocation.json"
        invoke.write_bytes(encoded(invocation))
        invoke.chmod(0o600)
        output = scratch / "package"
        result = run_worker("adjudicate", "--invocation", invoke, "--output", output)
        sha = result.get("manifest_sha256") if type(result) is dict else None
        validate_result(result, request, sha)
        replay = run_worker("adjudicate-verify", "--package", output,
                            "--manifest-sha256", sha, "--request", path)
        require(result == replay, "live report provider differs from offline reconstruction")
        sync_package(output)
        destination = packages / sha
        if destination.exists():
            replay = replay_package(root, {"path": f"reports/{sha}/manifest.json", "sha256": sha}, request)
            require(replay == result, "retained report package differs")
        else:
            os.rename(output, destination)
            fd = os.open(packages, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    package = {"path": f"reports/{sha}/manifest.json", "sha256": sha}
    ref, gated = retain_report(root, source, request, package,
                               allocation_inputs(adapter, attempt_height), selection=selection,
                               reward=selected_reward(adapter, manifest["day"]),
                               reward_transition=transition_reference(adapter, manifest["day"]))
    adapter._calculation_reference = ref
    return gated


def sync_package(directory):
    """Commit provider-owned, already validated raw files before linking R1."""
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "report package directory refused")
    directories = [root]
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "report package link refused")
        if path.is_dir():
            directories.append(path)
            continue
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(fd)
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
                    "report package member must be an unlinked regular file")
            os.fsync(fd)
        finally:
            os.close(fd)
    for path in reversed(directories):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def starter_input(adapter, source_reference, raw):
    """Bind report exclusions before Legacy pool allocation; never post-scale."""
    proof = verify_report(adapter._submitted_record_dir, source_reference)
    if proof is None:
        if enabled(adapter):
            manifest = verify(adapter._submitted_record_dir, source_reference)
            configuration = load_policy(adapter.report_gate_policy_path)
            bind_activation(adapter, configuration, persist=False)
            require(manifest["day"] < configuration["effective_day"],
                    "report-enabled starter source lacks report proof")
        return raw
    require(enabled(adapter), "new starter allocation cannot silently discard report policy")
    request = proof["request"]
    result = replay_package(adapter._submitted_record_dir, proof["package"], request)
    excluded = validate_result(result, request, proof["package"]["sha256"])
    bonus = proof.get("reward") == "bonus-20"
    if bonus:
        excluded = set()
        from ..reports.assignments import earns_reporting_bonus
    registrations = {row["uid"]: row for row in request["scored_seats"]}
    current = adapter.runner_chain.registration_blocks(raw["snapshot"]["finalizedBlock"])
    rows = []
    for seat in sorted(raw["snapshot"]["seats"], key=lambda r: r["uid"]):
        uid = seat["uid"]
        bound = registrations.get(uid)
        require(bound is not None and current.get(uid) == bound["registration_block"]
                and public_key_of(seat["hotkey"]).hex() == bound["hotkey"]
                and public_key_of(seat["coldkey"]).hex() == bound["coldkey"],
                "starter report identity changed since scoring")
        rows.append(dict(uid=uid, coldkey=seat["coldkey"], hotkey=seat["hotkey"],
                         registrationBlock=bound["registration_block"]))
    return {**raw, "schema": "noah-starter-epoch-preview/5",
            "reportGate": {"schema": "noah-starter-report-exclusions/1",
                           "proofSha256": source_reference["sha256"],
                           "evalDay": request["eval_day"], "registrations": rows,
                           "exclusions": [row for row in rows if row["uid"] in excluded],
                           **({"bonusUids": sorted(r["uid"] for r in result["scored_decisions"]
                                                   if earns_reporting_bonus(r, request))} if bonus else {})}}


def check_before_send(adapter, reference, vector):
    """A new send uses the current selected policy and the original frozen proof."""
    root = adapter._submitted_record_dir
    manifest = verify(root, reference)
    context = artifact(root, manifest, "context")
    if "burn_allocation" in context:
        from .burn_publication import report_subject
        reference, vector = report_subject(root, reference, vector)
        manifest = verify(root, reference)
        context = artifact(root, manifest, "context")
    if "report_no_bonus" in context:
        from .weekly_runtime import verify_no_bonus, bind_template
        proof = verify_no_bonus(root, reference)
        require(enabled(adapter) and getattr(adapter, "weekly_assignments", False), "new no-bonus send requires selected weekly mode")
        configuration = load_policy(adapter.report_gate_policy_path)
        bind_activation(adapter, configuration, persist=True)
        bind_template(root, configuration)
        require(proof["configuration"] == configuration
                and selected_reward(adapter, manifest["day"]) == "bonus-20"
                and proof["reward_transition"] == transition_reference(adapter, manifest["day"])
                and manifest["publishable_weights_u16"] == {str(k): v for k, v in vector.items()},
                "no-bonus send differs from frozen policy or allocation")
        return  # Ordinary V2 caller still verifies twin calculation, current identities and expiry.
    if not enabled(adapter) and "report_gate" not in context:
        return
    proof = verify_report(root, reference)
    require(enabled(adapter), "new send lacks selected report enforcement")
    configuration = load_policy(adapter.report_gate_policy_path)
    if proof is not None:
        require(configuration["effective_day"] == proof["request"]["policy"]["effective_day"],
                "new report cutoff would bypass an existing frozen proof")
    bind_activation(adapter, configuration, persist=True)
    if manifest["day"] < configuration["effective_day"]:
        require(proof is None, "new report cutoff would bypass an existing frozen proof")
        return  # Pre-effective financial days keep their original allocation.
    require(proof is not None, "new send lacks selected report enforcement")
    require(proof.get("reward", "gate") == selected_reward(adapter, manifest["day"]),
            "report reward differs from frozen calculation")
    require(proof.get("reward_transition") == transition_reference(adapter, manifest["day"]),
            "report reward transition differs from frozen calculation")
    request = proof["request"]
    configuration = configuration_for_day(adapter, configuration, manifest["day"])
    if configuration["schema"] == "noah.weekly-assignment-template/2":
        from .weekly_runtime import bind_template
        bind_template(root, configuration)
        require(proof.get("selection", {}).get("configuration_sha256") == semantic_digest(configuration),
                "current weekly policy differs from frozen report selection")
        verify_selection(proof["selection"], request)
        selected = request["policy"]
    elif configuration["schema"] == "noah.report-schedule/1":
        require(proof.get("selection", {}).get("configuration_sha256") == semantic_digest(configuration),
                "current schedule differs from frozen report selection")
        selected, _ = select_policy(configuration, list(adapter.runner_chain.endpoints.values()),
                                     request["eval_day"], adapter.day_anchor_ts)
    else:
        selected = policy_for_day(configuration, request["eval_day"], adapter.day_anchor_ts)
    require(semantic_digest(selected) == request["policy_sha256"],
            "new send report policy differs from frozen calculation")
    require(manifest["publishable_weights_u16"] == {str(k): v for k, v in vector.items()}
            and any(vector.values()), "new send differs from report allocation or has no eligible weight")
    chain = adapter.runner_chain
    evaluation = request["evaluation"]
    head = chain.head_block()
    require(head >= evaluation["block_number"]
            and chain.block_hash(evaluation["block_number"]).removeprefix("0x") == evaluation["block_hash"]
            and chain.genesis_hash().removeprefix("0x") == request["genesis_hash"]
            and adapter.netuid == request["mining_netuid"], "report evaluation identity changed")
    paid = {uid for uid, value in vector.items() if value}
    seats = {row["uid"]: row for row in request["scored_seats"]}
    hotkeys = chain.seat_hotkeys(head)
    registrations = chain.registration_blocks(head)
    owners = chain.hotkey_owners([hotkeys[uid] for uid in paid if uid in hotkeys], head)
    require(paid <= set(seats), "new send includes an unscored report identity")
    for uid in paid:
        seat = seats[uid]
        hot = hotkeys.get(uid)
        require(hot is not None and public_key_of(hot).hex() == seat["hotkey"]
                and owners.get(hot) is not None and public_key_of(owners[hot]).hex() == seat["coldkey"]
                and registrations.get(uid) == seat["registration_block"],
                "report payout miner incarnation changed before signing")
