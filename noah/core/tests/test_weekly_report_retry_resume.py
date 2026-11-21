"""Regression for later-height retries with immutable weekly evidence."""

import copy
import json
from pathlib import Path

import pytest

from noah.chain.ss58 import address_of
from noah.core import report_publication, report_runtime as runtime
from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.report_publication import ReportPublicationRefused
from noah.core.tests.test_weekly_runtime import prepared, unsigned_week
from noah.core.weekly_runtime import bind_template, marker_path, verify_no_bonus
from noah.points.publication import check_before_send
from noah.points.run import agreed
from noah.reports.assignments import retain_week, sign_week
from noah.reports.schedule import BOUNDARY, LEGACY_BOUNDARY, _resolve
from noah.reports.tests.test_schedule import ANCHOR, Chain


def later_r0(p, changed=None):
    original = verify(p.root, p.raw)
    context = artifact(p.root, original, 'context')
    fixture = artifact(p.root, original, 'fixture')
    score = agreed(fixture)
    assert score == artifact(p.root, original, 'agreed_score')
    vector = dict(p.vector)
    context['height'] += 1
    if changed == 'fixture':
        fixture['name'] += '-changed'
    elif changed == 'score':
        score['miners'][0]['payWeightFp'] = '1'
    elif changed == 'raw-vector':
        vector[3] -= 1
    elif changed == 'calendar':
        context['day_start_utc_unix'] += 1
    elif changed == 'candidate':
        context['candidate'] = {'source_sha256': 'aa' * 32}
    elif changed == 'roster':
        context['scored_hotkeys']['3'] = address_of(bytes([8]) * 32)
    reference = retain(p.root, fixture=fixture, agreed_text=encoded(score).decode(),
                       vector=vector, context=context, canonical_day=changed is None)
    assert reference != p.raw
    restarted = copy.copy(p.a)
    restarted._calculation_reference = reference
    return restarted, reference, vector


def signed_week_setup(p, monkeypatch, *, boundary_schema=BOUNDARY):
    bind_template(p.root, p.configuration, persist=True)
    key = p.configuration['assignment_signer_public_key']
    week = sign_week(unsigned_week(p), p.pair, trusted_public_key=key)
    retain_week(p.root / 'report-state' / 'weekly-assignments', week, trusted_public_key=key)
    chain = Chain(shift=p.a.day_anchor_ts - ANCHOR, head=16000)
    monkeypatch.setattr(runtime, 'resolve_schedule',
                        lambda policy, endpoints, day: _resolve(policy, [chain, chain], day, boundary_schema=boundary_schema))
    runner = p.a.runner_chain
    assert len(runner.endpoints) == 2
    runner.head_block = lambda: 16000
    runner.block_hash = chain.block_hash
    runner.block_timestamp = lambda block: p.a.day_anchor_ts + block * 12
    runner.block_timestamp_ms = lambda block: runner.block_timestamp(block) * 1000
    runner.registration_blocks = lambda block: {3: 1}
    return p.root / 'report-state' / f'day-{p.day}.request.json'


def result_for(request, manifest_sha):
    round_shas = [runtime.semantic_digest(row) for row in request['policy']['required_rounds']]
    seat, = request['scored_seats']
    decision = {key: seat[key] for key in ('uid', 'hotkey', 'coldkey', 'registration_block')}
    decision.update(effect='preserve', outcome='verified', rounds=[
        dict(round_sha256=sha, effect='preserve', outcome='verified',
             status='VERIFIED', reason='exact_match')
        for sha in round_shas
    ])
    return dict(
        schema='noah.report-adjudication-result/1', manifest_sha256=manifest_sha,
        request_sha256=runtime.semantic_digest(request), policy_sha256=request['policy_sha256'],
        status='COMPLETE', scored_decisions=[decision], rounds=[
            dict(round_sha256=sha, required_roster_sha256='dd' * 32,
                 evaluation=request['evaluation'], readers={'reader_a': 'ee' * 32, 'reader_b': 'ff' * 32},
                 engine_sha256='01' * 32, calculation_sha256='02' * 32,
                 verdicts_sha256='03' * 32, resolved=True)
            for sha in round_shas
        ],
    )


def open_unresolved_intent(p):
    intent = p.a._publish_intent
    context = artifact(p.root, verify(p.root, p.raw), 'context')
    intent.open(day=p.day, height=context['height'], hotkey=context['designated_validator_hotkey'],
                netuid=p.a.netuid, mode='direct', exclusive=True)
    return intent.path, intent.path.read_bytes()


@pytest.mark.parametrize('changed', [None, 'open-intent', 'policy', 'candidate'])
@pytest.mark.parametrize('upgrade_boundary', [False, True])
def test_provider_outage_restart_at_later_height_resumes_original_request(prepared, monkeypatch, changed, upgrade_boundary):
    p = prepared
    request_path = signed_week_setup(p, monkeypatch, boundary_schema=LEGACY_BOUNDARY if upgrade_boundary else BOUNDARY)
    collected = []
    replies = []
    allocation_reads = []
    initial_calls = len(p.client.calls)

    def worker(action, *args):
        if action == 'adjudicate':
            request = json.loads(Path(args[1]).read_bytes())['request']
            collected.append(request)
            if len(collected) == 1:
                raise ReportPublicationRefused('report worker could not complete')
            destination = Path(args[3])
            destination.mkdir()
            manifest = encoded({'stub': 'isolated worker boundary'})
            (destination / 'manifest.json').write_bytes(manifest)
            replies.append(result_for(request, report_publication.digest(manifest)))
        else:
            assert action == 'adjudicate-verify'
            assert json.loads(Path(args[-1]).read_bytes()) == collected[0]
        return copy.deepcopy(replies[0])

    monkeypatch.setattr(runtime, 'run_worker', worker)
    monkeypatch.setattr(report_publication, 'run_worker', worker)
    with pytest.raises(ReportPublicationRefused, match='could not complete'):
        runtime.prepare_daily(p.a, p.vector)
    frozen_bytes = request_path.read_bytes()
    assert collected[0]['calculation']['reference'] == p.raw

    if upgrade_boundary:
        chain = Chain(shift=p.a.day_anchor_ts - ANCHOR, head=16000)
        monkeypatch.setattr(runtime, 'resolve_schedule',
                            lambda policy, endpoints, day: _resolve(policy, [chain, chain], day))
    restarted, later, vector = later_r0(p, 'candidate' if changed == 'candidate' else None)
    if changed is not None:
        if changed == 'open-intent':
            intent_path, intent_bytes = open_unresolved_intent(p)
        elif changed == 'policy':
            config = {**p.configuration, 'commit_seconds': p.configuration['commit_seconds'] + 1}
            Path(p.a.report_gate_policy_path).write_bytes(encoded(config))
        with pytest.raises(ReportPublicationRefused, match='unresolved publication intent' if changed == 'open-intent' else None):
            runtime.prepare_daily(restarted, vector)
        assert collected == [collected[0]]
        assert request_path.read_bytes() == frozen_bytes
        if changed == 'open-intent':
            assert intent_path.read_bytes() == intent_bytes
        return

    attempt_height = verify(p.root, later)['height']
    hotkeys = artifact(p.root, verify(p.root, later), 'context')['scored_hotkeys']
    def fixture_hotkeys(height):
        allocation_reads.append(height)
        return {int(uid): hotkey for uid, hotkey in hotkeys.items()}
    restarted.fixture_hotkeys = fixture_hotkeys
    restarted.live_uids = lambda height: sorted(restarted.runner_chain.seat_hotkeys(height))
    final = runtime.prepare_daily(restarted, vector)
    assert final == vector
    assert allocation_reads == [attempt_height]
    assert collected == [collected[0], collected[0]]
    assert request_path.read_bytes() == frozen_bytes
    proof = report_publication.verify_report(p.root, restarted._calculation_reference)
    assert proof['source'] == p.raw and proof['request'] == collected[0]
    assert proof['selection']['resolution']['base']['boundary_proof']['schema'] == BOUNDARY
    assert verify(p.root, later)['height'] == attempt_height
    runtime.check_before_send(restarted, restarted._calculation_reference, final)
    check_before_send(restarted, restarted._calculation_reference, final)
    assert len(p.client.calls) == initial_calls and p.a._publish_intent.load() is None


@pytest.mark.parametrize('intent_open', [False, True])
def test_no_bonus_marker_resumes_original_r0_on_later_height(prepared, intent_open):
    p = prepared
    assert runtime.prepare_daily(p.a, p.vector) == p.vector
    marker = marker_path(p.root, p.day)
    frozen_bytes = marker.read_bytes()
    proof_ref = p.a._calculation_reference
    restarted, later, vector = later_r0(p)
    if intent_open:
        intent_path, intent_bytes = open_unresolved_intent(p)
        with pytest.raises(ReportPublicationRefused, match='unresolved publication intent'):
            runtime.prepare_daily(restarted, vector)
        assert intent_path.read_bytes() == intent_bytes
        assert marker.read_bytes() == frozen_bytes
        return
    assert runtime.prepare_daily(restarted, vector) == vector
    assert restarted._calculation_reference == proof_ref
    assert marker.read_bytes() == frozen_bytes
    assert verify_no_bonus(p.root, proof_ref)['source'] == p.raw
    assert verify(p.root, later)['height'] != verify(p.root, p.raw)['height']
    runtime.check_before_send(restarted, proof_ref, vector)
    check_before_send(restarted, proof_ref, vector)


@pytest.mark.parametrize('changed', ['fixture', 'score', 'raw-vector', 'calendar', 'candidate', 'roster'])
def test_no_bonus_marker_refuses_changed_r0(prepared, changed):
    p = prepared
    runtime.prepare_daily(p.a, p.vector)
    marker = marker_path(p.root, p.day)
    frozen_bytes = marker.read_bytes()
    restarted, _, vector = later_r0(p, changed)
    with pytest.raises(ReportPublicationRefused):
        runtime.prepare_daily(restarted, vector)
    assert marker.read_bytes() == frozen_bytes


@pytest.mark.parametrize('changed', ['backward-height', 'reference-metadata', 'derived-proof',
                                     'corrupt-intent', 'same-source-intent'])
def test_frozen_no_bonus_resume_rejects_invalid_provenance_and_uncertain_sends(prepared, changed):
    from noah.core.publish_intent import PublicationStatusUnknown
    p = prepared
    runtime.prepare_daily(p.a, p.vector)
    marker = marker_path(p.root, p.day)
    frozen_bytes = marker.read_bytes()
    restarted, later, vector = later_r0(p)
    manifest = verify(p.root, later)
    context = artifact(p.root, manifest, 'context')
    if changed in ('backward-height', 'derived-proof'):
        if changed == 'backward-height':
            context['height'] = verify(p.root, p.raw)['height'] - 1
        else:
            context['report_gate'] = {'untrusted': 'derived calculation cannot be a raw retry'}
        restarted._calculation_reference = retain(
            p.root, fixture=artifact(p.root, manifest, 'fixture'),
            agreed_text=encoded(artifact(p.root, manifest, 'agreed_score')).decode(),
            vector=vector, context=context, canonical_day=False)
    elif changed == 'reference-metadata':
        restarted._calculation_reference = {**later, 'day': later['day'] + 1}
    elif changed == 'corrupt-intent':
        p.a._publish_intent.path.write_bytes(b'not valid intent JSON')
    else:
        restarted._calculation_reference = p.raw
        p.a._publish_intent.open(day=p.day, height=manifest['height'],
                                hotkey=p.a.wallet_hotkey.ss58_address, netuid=p.a.netuid)
    before = {f: f.read_bytes() for f in p.root.rglob('*.json')}
    intent_bytes = p.a._publish_intent.path.read_bytes() if p.a._publish_intent.path.exists() else None
    with pytest.raises(PublicationStatusUnknown):
        runtime.prepare_daily(restarted, vector)
    assert marker.read_bytes() == frozen_bytes
    assert all(path.read_bytes() == data for path, data in before.items())
    if intent_bytes is not None:
        assert p.a._publish_intent.path.read_bytes() == intent_bytes
