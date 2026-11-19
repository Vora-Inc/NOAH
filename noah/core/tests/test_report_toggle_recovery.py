"""Offline report ON -> OFF recovery through real adapters and durable proofs.

Only the provider replay response and network/signing transport are synthetic,
following test_report_publication and _prepared_transport_fake. Calculation,
report/starter proof validation, independent starter allocation, intent storage,
receipt reconciliation and new-send checks execute their production code.
"""

import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from noah.chain.ss58 import address_of, public_key_of
from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.publish import NotOnChain
from noah.core.report_publication import ReportPublicationRefused, retain_report, semantic_digest
from noah.core.report_runtime import ELIGIBILITY, starter_input
from noah.core.tests._prepared_transport_fake import PreparedTransportFake, install
from noah.core.tests.test_direct_runtime475 import adapter
from noah.core.tests.test_epoch_work import EpochClient
from noah.core.tests.test_report_publication import scenario
from noah.points import onboarding
from noah.points.run import agreed
from noah.points.tests.test_no_pool import economics_fixture
from noah.reports.adjudication_contract import validate_request
from noah.research.tests.test_starter_epoch import sync
from noah.starter.allocation import preview
from noah.starter.publication import retain_overlay
from noah.starter.runtime import identity
from noah.starter.tests.test_epoch_clock import request_live


def _fixture(mode):
    raw = request_live()
    for index, miner in enumerate(raw['fixture']['miners']):
        uid = index + 3
        hot = address_of(bytes([uid + 10]) * 32)
        cold = address_of(bytes([uid + 20]) * 32)
        miner.update(uid=uid, coldkey=cold)
        raw['snapshot']['seats'][index].update(uid=uid, coldkey=cold, hotkey=hot,
                                              observedBlock=201)
    raw['scoredHotkeys'] = {str(s['uid']): s['hotkey'] for s in raw['snapshot']['seats']}
    raw['lifetimeEarnings'] = []
    sync(raw)
    raw['snapshot'].update(genesisHash='0x' + '11' * 32, epoch=200,
                           nextEpochBlock=560, finalizedBlock=201,
                           finalizedHash=f'0x{201:064x}', weightEffectiveEpoch=560,
                           pendingThroughEpoch=559)
    if mode == 'legacy':
        return raw, raw['fixture'], preview(raw)['ordinaryOutput']
    fixture = economics_fixture([[(100 * 10**9, 0, 0), (0, 75 * 10**9, 0)]] * 2)
    for index, miner in enumerate(fixture['miners']):
        miner.update(uid=index + 3, coldkey=raw['snapshot']['seats'][index]['coldkey'])
    fixture.update(pointsHotkeys=dict(raw['scoredHotkeys']),
                   pointsOwners={str(s['uid']): s['coldkey'] for s in raw['snapshot']['seats']},
                   pointsRules=onboarding.RULES, pointsExcludedUids=[0])
    return None, fixture, agreed(fixture)


def _ready(tmp_path, monkeypatch, mode, overlay, reward="gate"):
    install(monkeypatch)
    a, client = adapter(tmp_path, EpochClient())
    client.block = client.finalized_head = 201
    raw, fixture, score = _fixture(mode)
    day, anchor = int(fixture['evalDay']), int(fixture['dayEpochTs'])
    hotkeys = (raw['scoredHotkeys'] if raw else fixture['pointsHotkeys'])
    coldkeys = {str(m['uid']): m['coldkey'] for m in fixture['miners']}
    a.scoring_mode = mode
    a.day_anchor_ts = anchor
    a.ark_coldkey = address_of(bytes.fromhex('55' * 32))
    a.designated_validator_hotkey = address_of(bytes.fromhex('66' * 32))
    a._day_window = lambda _: (day, 1, 99)
    a.epoch_republication = True
    a.runner_chain.genesis_hash = lambda: '0x' + '11' * 32
    a.runner_chain.block_timestamp = lambda _: anchor + (day + 1) * 86400 + 100
    a.runner_chain.seat_hotkeys = lambda _: {int(k): v for k, v in hotkeys.items()}
    a.runner_chain.hotkey_owners = lambda *_: {hotkeys[k]: v for k, v in coldkeys.items()}
    a.runner_chain.registration_blocks = lambda _: {int(k): 50 for k in hotkeys}
    a.runner_chain.starter_epoch_info = lambda _: dict(LastEpochBlock=200, Tempo=360, PendingEpochAt=0)
    a.owner_uids = lambda: {0}
    a.starter_entry_policy = SimpleNamespace(snapshot={'sha256': 'policy'}) if overlay else None
    if mode == onboarding.MODE:
        a.points_policy = SimpleNamespace(manifest=dict(policy=onboarding.POLICY,
                                                        sha256='a' * 64, cutoff_day=0))
    context = dict(day=day, height=99, to_block=99, to_block_hash=f'0x{99:064x}',
                   genesis_hash=a.runner_chain.genesis_hash(), netuid=82,
                   ark_address=a.ark_coldkey, designated_validator_hotkey=a.designated_validator_hotkey,
                   scoring_mode=mode, points_policy=getattr(getattr(a, 'points_policy', None), 'manifest', None),
                   day_start_utc_unix=anchor + day * 86400,
                   scored_hotkeys=hotkeys, scored_coldkeys=coldkeys)
    ordinary = {int(k): v for k, v in score['weightsU16'].items()}
    source = retain(a._submitted_record_dir, fixture=fixture, agreed_text=encoded(score).decode(),
                    vector=ordinary, context=context)

    request, result, _, _ = scenario()
    delta = (anchor + day * 86400 - request['day_anchor_ts']) * 1000
    request.update(eval_day=day, day_anchor_ts=anchor)
    request['policy'].update(effective_day=day, eligibility_policy_version=ELIGIBILITY)
    contract = request['policy']['required_rounds'][0]
    contract['eligibility_policy_version'] = ELIGIBILITY
    for key in list(contract):
        if key.endswith('_ms'):
            contract[key] += delta
    contract['round_date'] = datetime.fromtimestamp(contract['block_timestamp_ms'] / 1000,
                                                   timezone.utc).date().isoformat()
    request['policy_sha256'] = semantic_digest(request['policy'])
    request['evaluation'].update(block_hash=f'{110:064x}', timestamp_ms=contract['reveal_close_ms'])
    template = request['scored_seats'][0]
    request['scored_seats'] = [dict(template, uid=int(uid), hotkey=public_key_of(hot).hex(),
                                   coldkey=public_key_of(coldkeys[uid]).hex())
                               for uid, hot in hotkeys.items()]
    request['calculation'] = dict(reference=source,
        fixture_sha256=verify(a._submitted_record_dir, source)['artifacts']['fixture']['sha256'])
    validate_request(request)
    round_sha = semantic_digest(contract)
    decisions = []
    for seat in request['scored_seats']:
        # Preserve the ordinary miner, exclude the second seat; Legacy R2 must
        # keep that failed newcomer out of its independent starter allocation.
        failed = seat['uid'] == 4
        decisions.append({**{k: seat[k] for k in ('uid', 'hotkey', 'coldkey', 'registration_block')},
            'effect': 'zero' if failed else 'preserve',
            'outcome': 'confirmed_failure' if failed else 'verified',
            'rounds': [dict(round_sha256=round_sha, effect='zero' if failed else 'preserve',
                            outcome='confirmed_failure' if failed else 'verified',
                            status='MISMATCH' if failed else 'VERIFIED',
                            reason='outputs_differ' if failed else 'exact_match')]})
    result.update(request_sha256=semantic_digest(request), policy_sha256=request['policy_sha256'],
                  scored_decisions=decisions)
    result['rounds'][0].update(round_sha256=round_sha, evaluation=request['evaluation'])
    replay_calls = []

    def replay(root, package, frozen_request):
        assert frozen_request == request
        assert package['sha256'] == result['manifest_sha256']
        replay_calls.append(copy.deepcopy(frozen_request))
        return copy.deepcopy(result)

    monkeypatch.setattr('noah.core.report_publication.replay_package', replay)
    monkeypatch.setattr('noah.core.report_runtime.replay_package', replay)
    policy_path = tmp_path / 'policy.json'
    policy_path.write_text(json.dumps(request['policy']))
    a.report_gate_policy_path = str(policy_path)
    a.report_gate_enabled = True
    a.report_reward = reward
    package = dict(path=f"reports/{result['manifest_sha256']}/manifest.json",
                   sha256=result['manifest_sha256'])
    submission = dict(live_uids=[3, 4], owner_uids=[0], fixture_hotkeys=hotkeys, chain_hotkeys=hotkeys)
    bound, vector = retain_report(a._submitted_record_dir, source, request, package, submission, reward=reward)
    if overlay:
        enriched = starter_input(a, bound, raw)
        bound = retain_overlay(a._submitted_record_dir, bound, enriched)
        vector = {int(k): v for k, v in verify(a._submitted_record_dir, bound)['publishable_weights_u16'].items()}
        (a._submitted_record_dir / 'starter-guard.json').write_text(json.dumps(dict(
            identity=identity(a), observed_at=time.time(), status='idle', head=201)))
    a._calculation_reference = bound
    return a, client, source, bound, vector, ordinary, replay_calls, policy_path


@pytest.mark.parametrize('reward', ['gate', 'bonus-20'])
@pytest.mark.parametrize('mode,overlay', [('legacy', False), ('legacy', True),
                                        (onboarding.MODE, False)],
                         ids=['legacy-r1', 'legacy-r2', 'v2-r1'])
def test_pending_report_proof_reconciles_unsigned_after_off(tmp_path, monkeypatch, mode, overlay, reward):
    a, client, source, bound, vector, ordinary, replays, policy_path = _ready(
        tmp_path, monkeypatch, mode, overlay, reward)
    if reward == "gate" or (mode == "legacy" and not overlay):
        assert vector[4] == 0
    else:
        assert vector[4] > 0
    before_files = {p: p.read_bytes() for p in a._submitted_record_dir.rglob('*') if p.is_file()}
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        a.publish_direct(vector, 99)
    original = copy.deepcopy(a._publish_intent.load())
    assert original['calculation_evidence'] == bound
    assert original['vector'] == {str(k): v for k, v in vector.items()}
    assert len(client.calls) == 1
    a.report_gate_enabled = False
    policy_path.unlink()  # Historical recovery cannot depend on current policy.
    a._calculation_reference = None
    client.lost_reply = False
    replay_count = len(replays)

    def forbidden(*args, **kwargs):
        pytest.fail('OFF reconciliation/refusal attempted signing, submission, or current report policy')

    with monkeypatch.context() as blocked:
        blocked.setattr(PreparedTransportFake, 'prepare', forbidden)
        blocked.setattr(PreparedTransportFake, 'send', forbidden)
        blocked.setattr(a.publisher, 'send_context', forbidden)
        blocked.setattr('noah.core.report_runtime.load_policy', forbidden)
        assert a._already_published_direct(99) is True
        assert a._publish_intent.load() is None
        assert len(replays) > replay_count
        record_path = a._submitted_record_dir / f"day-{source['day']}.submitted.json"
        record = json.loads(record_path.read_text())
        retained = record['direct_intent']
        assert {k: v for k, v in retained.items() if k != 'result'} == {
            k: v for k, v in original.items() if k != 'result'}
        assert original['result']['indeterminate'] is True
        assert retained['result']['block'] == 202
        assert record['calculation_evidence'] == bound
        assert record['submitted_weights_u16'] == original['vector']
        assert record['applied_weights_readback']['confirmed'] is True
        # Use a fresh publication slot so the report OFF check, not duplicate
        # daily publication detection, must reject the old bound reference.
        a._calculation_reference = bound
        with pytest.raises(ReportPublicationRefused, match='lacks selected report enforcement'):
            a.publish_direct(vector, 99, refresh_epoch=200, source_day=source['day'])
        assert a._publish_intent.load() is None
    assert len(client.calls) == 1
    assert all(p.read_bytes() == content for p, content in before_files.items())

    # A fresh ordinary calculation uses the same financial inputs, has no report
    # binding, and must work OFF without opening a policy or invoking a provider.
    manifest = verify(a._submitted_record_dir, source)
    context = artifact(a._submitted_record_dir, manifest, 'context')
    context['height'] = 100
    fresh = retain(a._submitted_record_dir, fixture=artifact(a._submitted_record_dir, manifest, 'fixture'),
                   agreed_text=encoded(artifact(a._submitted_record_dir, manifest, 'agreed_score')).decode(),
                   vector=ordinary, context=context, canonical_day=False)
    assert fresh != source
    a._calculation_reference = fresh
    client.block = client.finalized_head
    with monkeypatch.context() as blocked:
        blocked.setattr('noah.core.report_runtime.load_policy', forbidden)
        blocked.setattr('noah.core.report_publication.replay_package', forbidden)
        blocked.setattr('noah.core.report_runtime.replay_package', forbidden)
        assert a.prepare_report_vector(ordinary) is ordinary
        receipt = a.publish_direct(ordinary, 100, refresh_epoch=200, source_day=source['day'])
    assert receipt['calculation_evidence'] == fresh
    assert receipt['applied_weights_readback']['confirmed'] is True
    assert len(client.calls) == 2
    assert a._publish_intent.load() is None


@pytest.mark.parametrize('mode,overlay', [('legacy',False),('legacy',True),(onboarding.MODE,False)])
def test_bonus_epoch_lost_reply_recovers_after_midnight_from_frozen_proof(tmp_path,monkeypatch,mode,overlay):
    a,client,source,bound,vector,ordinary,replays,policy_path=_ready(tmp_path,monkeypatch,mode,overlay,'bonus-20')
    receipt=a.publish_direct(vector,99)
    assert receipt['applied_weights_readback']['confirmed']
    client.block=client.finalized_head=203
    client.lost_reply=True
    with pytest.raises(NotOnChain):
        a.publish_direct(vector,99,refresh_epoch=200,source_day=source['day'])
    intent=copy.deepcopy(a._publish_intent.load())
    count=len(client.calls)
    # Clear process-local calculation, advance the wall clock, and remove current
    # policy. Reconciliation must use immutable request/result/receipt evidence.
    a._calculation_reference=None
    a.report_gate_enabled=False
    policy_path.unlink()
    a.runner_chain.block_timestamp=lambda _: a.day_anchor_ts+(source['day']+2)*86400+1
    client.lost_reply=False
    with monkeypatch.context() as blocked:
        blocked.setattr(PreparedTransportFake,'prepare',lambda *_:pytest.fail('recovery signed again'))
        blocked.setattr(PreparedTransportFake,'send',lambda *_:pytest.fail('recovery resubmitted'))
        assert a._already_published_direct(99)
    assert a._publish_intent.load() is None
    assert len(client.calls)==count
    folder = f"starter-publications/200/{intent['intent_id']}" if overlay else "epoch-publications/200"
    saved=json.loads((a._submitted_record_dir/f"{folder}/day-{source['day']}.submitted.json").read_text())
    assert saved['direct_intent']['intent_id']==intent['intent_id']
    assert saved['calculation_evidence']==bound
    assert saved['submitted_weights_u16']==intent['vector']
    assert saved['applied_weights_readback']['confirmed']


def test_bonus_new_send_refuses_reward_switch_but_preserves_evidence(tmp_path,monkeypatch):
    a,client,source,bound,vector,*_=_ready(tmp_path,monkeypatch,onboarding.MODE,False,'bonus-20')
    before={p:p.read_bytes() for p in a._submitted_record_dir.rglob('*') if p.is_file()}
    a.report_reward='gate'
    with pytest.raises(ReportPublicationRefused,match='reward differs'):
        a.publish_direct(vector,99)
    assert not client.calls and a._publish_intent.load() is None
    assert all(p.read_bytes()==b for p,b in before.items())
