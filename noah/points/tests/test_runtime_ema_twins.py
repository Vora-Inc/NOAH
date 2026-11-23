"""Full V5 twins with actual Store-derived, post-boundary EMA credit receipts."""
import copy

import pytest

from noah.points import onboarding
from noah.points.ema_reference import digest
from noah.points.engine import evaluate
from noah.points.run import agreed, typescript_text
from noah.points.tests import test_ema_reference as reference

# Independent expected tuples, not imported from either engine's admission map.
CODES = {
    483: '0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2',
    488: '0x3147daab804046184359d8a929cb333d75f490323b9e1327887a172c64eafa94',
    490: '0x32a16fe6163e49ae26ade2413e80b14d641c56ab4d3246d4d39013b688369d54',
    491: '0x4201278656a17aff924ca24123c8e5866d569a84a7150bb0372bc7078fc6173c',
    492: '0x22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e',
    493: '0x51adea39b16a2b9d60b58d430246d49312f1111b74607edc1166e47a73de4591',
    494: '0x1735bc231789bead3ab3edfa583f3c9a95aaf7643adcc906ea224a7761a303ca',
}
BASE_CHAIN = reference.EmaChain


def fixture(tmp_path, monkeypatch, spec, parent=None):
    class ReviewedChain(BASE_CHAIN):
        def token_reference_context(self, block, assets):
            context = super().token_reference_context(block, assets)
            version = parent if parent is not None and block < 50 else spec
            context['runtime'] = {'spec_version': version, 'code_hash': CODES[version],
                                  'transaction_version': 2 if version == 494 else 1}
            context['parent_runtime'] = dict(context['runtime'])
            return context
    monkeypatch.setattr(reference, 'EmaChain', ReviewedChain)
    raw, policy = reference.ema_fixture(tmp_path)
    raw.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    return raw, policy


@pytest.mark.parametrize('spec', [483, 488, 490, 491, 492, 493, 494])
def test_full_v5_new_ema_credit_agrees_in_both_engines(tmp_path, monkeypatch, spec):
    raw, policy = fixture(tmp_path, monkeypatch, spec)
    assert len(raw['pointsReceipts']) == 2
    assert all(row['reason'] == 'ema-spot-reference' and row['status'] == 'accepted'
               and row['block'] >= raw['pointsPricingBoundary']['effectiveBlock']
               for row in raw['pointsReceipts'])
    assert sum(int(row['credit_rao']) for row in raw['pointsReceipts']) == 10**9
    assert all(p['runtime']['spec_version'] == p['parent_runtime']['spec_version'] == spec
               for p in raw['pointsPricing'].values())
    result = agreed(raw)
    assert result['policy'] == onboarding.POLICY
    assert result['receiptDispositions'] == raw['pointsReceipts']
    assert policy.ema_collector.chain.calls['ema_snapshot'] == 1


@pytest.mark.parametrize(('spec', 'parent'), [(493, 492), (494, 493)])
def test_full_v5_upgrade_receipt_keeps_reviewed_parent_and_current_tuples(tmp_path, monkeypatch, spec, parent):
    raw, _ = fixture(tmp_path, monkeypatch, spec, parent=parent)
    assert all(p['runtime']['spec_version'] == spec and p['parent_runtime']['spec_version'] == parent
               for p in raw['pointsPricing'].values())
    assert agreed(raw)['policy'] == onboarding.POLICY


@pytest.mark.parametrize('role', ['runtime', 'parent_runtime'])
@pytest.mark.parametrize('fault', ['wrong-code', 'unreviewed-spec', 'wrong-transaction', 'lifecycle-only-486'])
def test_rehashed_unreviewed_runtime_proof_is_refused_by_both_engines(tmp_path, monkeypatch, role, fault):
    raw, _ = fixture(tmp_path, monkeypatch, 494, parent=493)
    changed = {}
    pins = {}
    for old, original in raw['pointsPricing'].items():
        proof = copy.deepcopy(original)
        if fault == 'wrong-code': proof[role]['code_hash'] = CODES[491]
        elif fault == 'unreviewed-spec': proof[role]['spec_version'] = 495
        elif fault == 'wrong-transaction': proof[role]['transaction_version'] = 3 - proof[role]['transaction_version']
        else: proof[role] = {'spec_version':486,'transaction_version':1,'code_hash':'0x49781ac4cfda0ac6cf6e53cfceea7c728394d9c28da7881f47e3fbfbc239800f'}
        new = digest(proof); pins[old] = new; changed[new] = proof
    raw['pointsPricing'] = changed
    for row in raw['pointsReceipts']:
        new = pins[row['reference_proof']['sha256']]
        row['reference_proof'] = {'path': 'points-reference-proofs/' + new + '.json', 'sha256': new}
    with pytest.raises(ValueError): evaluate(raw)
    with pytest.raises(ValueError): typescript_text(raw)
