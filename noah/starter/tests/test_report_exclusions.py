"""Pure report exclusions: no authenticated provider or live activation here."""

import copy

import pytest

from noah.core.publish_intent import PublicationStatusUnknown
from noah.research.tests.test_starter_epoch import request, sync
from noah.starter.allocation import SHARED_POOL_UNITS, U16, preview
from noah.starter.second_opinion import check


def report_input(excluded=(), n=2, second_ordinary=True):
    raw = request(n)
    raw['schema'] = 'noah-starter-epoch-preview/4'
    snap = raw['snapshot']
    del snap['epochZeroTimestamp']
    snap.update(epoch=900, nextEpochBlock=1261, weightEffectiveEpoch=1261,
                pendingThroughEpoch=1260, epochStartTimestamp=snap['finalizedTimestamp']-100)
    if second_ordinary:
        uid = n+1
        miner = copy.deepcopy(raw['fixture']['miners'][0])
        miner.update(uid=uid, coldkey='second-established')
        raw['fixture']['miners'].append(miner)
        raw['scoredHotkeys'][str(uid)] = 'second-established-hot'
        seat = copy.deepcopy(snap['seats'][0])
        seat.update(uid=uid, coldkey=miner['coldkey'], hotkey=raw['scoredHotkeys'][str(uid)])
        snap['seats'].append(seat)
    sync(raw)
    baseline = copy.deepcopy(raw)
    raw['schema'] = 'noah-starter-epoch-preview/5'
    rows = [dict(uid=s['uid'], coldkey=s['coldkey'], hotkey=s['hotkey'], registrationBlock=1)
            for s in snap['seats']]
    raw['reportGate'] = dict(schema='noah-starter-report-exclusions/1', proofSha256='a'*64,
                             evalDay=int(raw['fixture']['evalDay']), registrations=rows,
                             exclusions=[copy.deepcopy(r) for r in rows if r['uid'] in excluded])
    return raw, baseline


@pytest.mark.parametrize('excluded', [(), (0,), (1,), (0, 1), (1, 2), (0, 1, 2, 3)])
def test_both_allocations_and_fallback_exclude_without_changing_accounting(excluded):
    raw, baseline = report_input(excluded)
    original = preview(baseline)
    result = preview(raw)
    check(raw, None, result)
    assert result['ordinaryOutput'] == original['ordinaryOutput']
    assert result['state']['seats'] == original['state']['seats']
    assert result['state']['schema'] == original['state']['schema'] == 'noah-starter-state/4'
    for uid in excluded:
        assert result['proposedWeightsU16'].get(str(uid), 0) == 0
        assert result['ordinaryFallbackWeightsU16'].get(str(uid), 0) == 0
        assert next(r for r in result['starter'] if r['uid']==uid)['status']=='report-excluded'
    assert result['allocatedStarterWeightU16'] <= SHARED_POOL_UNITS
    if result['proposedWeightsU16']:
        assert sum(result['proposedWeightsU16'].values()) == U16
    else:
        assert result['ordinaryFallbackWeightsU16'] == {}
    if not excluded:
        for field in ('proposedWeightsU16', 'ordinaryFallbackWeightsU16', 'starter',
                      'requestedStarterWeightsU16', 'allocatedStarterWeightU16'):
            assert result[field] == original[field]


def test_excluding_ordinary_miner_never_enlarges_starter_cap():
    raw, _ = report_input((0,), n=16)
    result = preview(raw)
    check(raw, None, result)
    assert result['allocatedStarterWeightU16'] == SHARED_POOL_UNITS
    assert result['proposedWeightsU16']['0'] == 0
    assert result['proposedWeightsU16']['17'] == U16-SHARED_POOL_UNITS
    assert result['ordinaryFallbackWeightsU16']['17'] == U16


def test_remaining_starters_cannot_receive_entire_vector_when_ordinary_fails():
    raw, _ = report_input((0,), second_ordinary=False)
    result = preview(raw)
    check(raw, None, result)
    assert result['noOrdinaryWeight'] is True
    assert result['proposedWeightsU16'] == result['ordinaryFallbackWeightsU16'] == {}
    assert result['allocatedStarterWeightU16'] == 0


def test_exclusion_does_not_graduate_starter_and_prior_state_is_preserved():
    raw, baseline = report_input((1,))
    previous_input = copy.deepcopy(baseline)
    previous_input['snapshot']['finalizedBlock'] -= 1
    for row in previous_input['snapshot']['seats']:
        row['observedBlock'] -= 1
    previous = preview(previous_input)['state']
    result = preview(raw, previous)
    check(raw, previous, result)
    assert result['state']['seats'] == preview(baseline, previous)['state']['seats']
    assert result['state']['seats']['["wallet-1","hotkey-1"]']['terminal'] == ''


def test_old_schema_refuses_report_data_and_new_schema_requires_it():
    raw, baseline = report_input()
    baseline['reportGate'] = raw['reportGate']
    with pytest.raises(ValueError, match='exact fields'):
        preview(baseline)
    del raw['reportGate']
    with pytest.raises(ValueError, match='exact fields'):
        preview(raw)


BAD_GATE = [
    lambda g:g.update(extra=True),
    lambda g:g.update(schema='other'),
    lambda g:g.update(proofSha256='A'*64),
    lambda g:g.update(evalDay=True),
    lambda g:g.update(evalDay=2),
    lambda g:g.update(registrations=list(reversed(g['registrations']))),
    lambda g:g['registrations'].pop(),
    lambda g:g['registrations'].append(copy.deepcopy(g['registrations'][-1])),
    lambda g:g['registrations'][0].update(uid=True),
    lambda g:g['registrations'][0].update(registrationBlock=True),
    lambda g:g['registrations'][0].update(registrationBlock=1001),
    lambda g:g['registrations'][0].update(coldkey='crossed-owner'),
    lambda g:g['registrations'][0].update(hotkey='hotkey-1'),
    lambda g:g['registrations'][0].update(unexpected=0),
    lambda g:g['exclusions'][0].update(registrationBlock=2),
    lambda g:g['exclusions'][0].update(coldkey='crossed-owner'),
    lambda g:g['exclusions'].append(copy.deepcopy(g['exclusions'][0])),
    lambda g:g.update(exclusions={}),
]


@pytest.mark.parametrize('corrupt', BAD_GATE)
def test_both_implementations_refuse_crossed_or_ambiguous_identity_proofs(corrupt):
    raw, _ = report_input((0,))
    result = preview(raw)
    corrupt(raw['reportGate'])
    # Match the altered result field so JS must reject the input itself,
    # rather than merely detecting result-copy mismatch.
    result['reportGate'] = copy.deepcopy(raw['reportGate'])
    with pytest.raises(ValueError):
        preview(raw)
    with pytest.raises(PublicationStatusUnknown):
        check(raw, None, result)


@pytest.mark.parametrize('field', ['proposedWeightsU16', 'ordinaryFallbackWeightsU16'])
def test_independent_checker_rejects_resurrected_excluded_weight(field):
    raw, _ = report_input((0, 1))
    result = preview(raw)
    result[field]['0'] = 1
    result[field]['3'] -= 1
    with pytest.raises(PublicationStatusUnknown):
        check(raw, None, result)


def test_report_exclusion_does_not_prevent_real_lifetime_exhaustion():
    raw, _ = report_input((1,))
    raw['snapshot']['seats'][1]['spendableEarnedRao'] = 5_000_000_000
    sync(raw)
    baseline = copy.deepcopy(raw)
    baseline['schema'] = 'noah-starter-epoch-preview/4'
    del baseline['reportGate']
    result = preview(raw)
    check(raw, None, result)
    assert result['state']['seats'] == preview(baseline)['state']['seats']
    assert result['state']['seats']['["wallet-1","hotkey-1"]']['terminal'] == 'exhausted'
    assert result['proposedWeightsU16']['1'] == 0
