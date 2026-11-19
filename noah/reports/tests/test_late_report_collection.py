"""Synthetic SDK-shaped blocks through collector, selection and verification.

These exercise the real collector walk, extrinsic/event interpretation, frozen
evaluation proof, selector and verifier. Only backing arithmetic is stubbed.
The SDK-shaped fixture does not qualify real SCALE decoding, paired readers,
network transport or deployed/Testnet behavior.
"""
from dataclasses import replace
import hashlib

import pytest

from noah.reports.backing import collector
from noah.reports.backing.keys import ss58_encode, timestamp_now_key
from noah.reports.backing.protocol.preimage import REPORT_POLICY, ReportSubmission, Submission
from noah.reports.backing.select import select_for_miner
from noah.reports.tests.test_history import (
    SyntheticChain, T0, MINER, OTHER, _Query, _hash, _round, _engine,
)
from noah.reports.verification import verify_collected


def moment(number):
    return T0 + number * 12000


class ReportChain(SyntheticChain):
    def __init__(self):
        super().__init__(15)
        self.decoded = []

    def rpc_request(self, method, params=None, result_handler=None):
        params = params or []
        if method == 'chain_getBlockHash' and params:
            return {'result': _hash(int(params[0], 16) if isinstance(params[0], str) else params[0])}
        if method == 'state_getStorage':
            assert params[0] == '0x' + timestamp_now_key().hex()
            number = int(self.headers[params[1]]['number'], 16)
            return {'result': '0x' + moment(number).to_bytes(8, 'little').hex()}
        return super().rpc_request(method, params, result_handler)

    def query(self, module, storage, params=None, block_hash=None, **kwargs):
        if (module, storage) == ('SubtensorModule', 'Uids'):
            return _Query(0)
        if (module, storage) == ('SubtensorModule', 'Owner'):
            return _Query(ss58_encode(OTHER))
        if (module, storage) == ('System', 'Events'):
            self.decoded.append(int(self.headers[block_hash]['number'], 16))
        return super().query(module, storage, params, block_hash, **kwargs)

    def submit(self, number, raw, *, success=True, signer=MINER, netuid=82):
        block = _hash(number)
        index = len(self.extrinsics[block])
        self.extrinsics[block].append({'address': ss58_encode(signer), 'call': {
            'call_module': 'Commitments', 'call_function': 'set_commitment',
            'call_args': [{'name': 'netuid', 'value': netuid}, {'name': 'info',
                'value': {'fields': [{'Raw32' if len(raw) == 32 else 'BigRaw': '0x' + raw.hex()}]}}]}})
        self.events[block].append({'extrinsic_idx': index,
            'event_id': 'ExtrinsicSuccess' if success else 'ExtrinsicFailed'})


def case():
    chain = ReportChain()
    contract = _round(chain, 2)
    contract.update(eligibility_policy_version=REPORT_POLICY, commit_close_ms=moment(6),
                    reveal_open_ms=moment(6), reveal_close_ms=moment(10))
    payload = ReportSubmission(genesis_hash=bytes.fromhex(contract['genesis_hash']),
        hotkey=MINER, coldkey=OTHER, netuid=contract['reported_netuid'], mining_netuid=82,
        round_yyyymmdd=int(contract['round_date'].replace('-', '')), block_number=2,
        block_hash=bytes.fromhex(contract['block_hash']), out1_ppm=10,
        out2_rao=2000000, out3_rao=3000000,
        release_prefix=bytes.fromhex(contract['release_prefix']), salt=b'x' * 16)
    return chain, contract, payload


def pair(chain, payload, commit, reveal):
    raw = payload.encode()
    if commit is not None:
        chain.submit(commit, hashlib.sha256(raw).digest())
    if reveal is not None:
        chain.submit(reveal, raw)


def collect(chain, contract, monkeypatch, *, evaluation_number=11):
    monkeypatch.setattr(collector, 'compute', lambda *_a, **_k: (_engine(contract), {}))
    evaluation = {'block_number': evaluation_number, 'block_hash': _hash(evaluation_number)[2:],
                  'timestamp_ms': moment(evaluation_number)}
    result = collector._collect(chain, 'synthetic-sdk-fixture', [], contract, None,
        [MINER.hex()], lambda: 0, chain_source=object(), evaluation=evaluation,
        evaluation_ancestor_limit=20)
    evidence = result['evidence'][MINER.hex()]
    assert result['evaluation_proof']['evaluation'] == evaluation
    assert evidence['collection']['state'] == 'COMPLETE'
    return verify_collected(contract, evidence), evidence


@pytest.mark.parametrize('commit,reveal,status,reason', [
    (3, 7, 'VERIFIED', 'exact_match'),
    (6, 8, 'LATE', 'commit'),  # Exact commit boundary is closed.
    (7, 8, 'LATE', 'commit'),
    (3, 10, 'LATE', 'reveal'),  # Exact reveal boundary is closed.
    (3, 11, 'LATE', 'reveal'),
    (3, 4, 'EARLY', 'reveal'),
    (4, 3, 'EARLY', 'reveal'),  # Disclosure before commitment is still observable.
    (3, 12, 'MISSING', 'reveal'),  # Present on chain but beyond frozen evaluation.
    (None, 7, 'MISSING', 'commit'),
    (7, None, 'MISSING', 'commit'),  # Bare hash cannot establish this round.
])
def test_collector_classifies_only_observed_current_round_pairs(monkeypatch, commit, reveal, status, reason):
    chain, contract, payload = case()
    pair(chain, payload, commit, reveal)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == (status, reason)
    assert max(chain.decoded) == 11
    if reveal == 12:
        assert evidence['reveal'] is None and 12 not in chain.decoded


def test_earliest_timely_pair_wins_over_early_late_and_later_commits(monkeypatch):
    chain, contract, payload = case()
    pair(chain, payload, 3, 4)
    chain.submit(7, payload.encode())
    chain.submit(11, payload.encode())
    pair(chain, replace(payload, salt=b'y' * 16), 4, 6)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert verdict['status'] == 'VERIFIED'
    assert evidence['commit']['block_number'] == 3
    assert evidence['reveal']['block_number'] == 7


def test_later_valid_pair_cannot_replace_first_timely_commit(monkeypatch):
    chain, contract, payload = case()
    pair(chain, payload, 3, None)
    pair(chain, replace(payload, salt=b'y' * 16), 4, 7)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('MISSING', 'reveal')
    assert evidence['commit']['block_number'] == 3 and evidence['reveal'] is None


def test_earliest_observed_bound_diagnostic_pair_is_deterministic(monkeypatch):
    chain, contract, payload = case()
    # Earlier old-round pair cannot be attributed; then two bound late pairs.
    pair(chain, replace(payload, block_number=1), 6, 10)
    first = replace(payload, out1_ppm=11, salt=b'1' * 16)
    pair(chain, first, 7, 11)
    pair(chain, payload, 8, 10)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('LATE', 'commit')
    assert evidence['commit']['block_number'] == 7
    assert evidence['reveal']['data_hex'] == first.encode().hex()


def test_early_commit_is_classified_only_with_current_round_preimage(monkeypatch):
    chain, contract, payload = case()
    contract['commit_open_ms'] = moment(3)
    pair(chain, payload, 2, 7)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('EARLY', 'commit')
    assert evidence['commit']['block_number'] == 2


@pytest.mark.parametrize('field,value', [
    ('genesis_hash', b'a' * 32), ('hotkey', b'a' * 32),
    ('block_number', 1), ('block_hash', b'a' * 32),
    ('netuid', 9), ('mining_netuid', 2),
    ('release_prefix', b'a' * 16), ('round_yyyymmdd', 20261113),
])
@pytest.mark.parametrize('commit', [3, 7])
def test_diagnostic_fallback_cannot_attribute_another_round(monkeypatch, field, value, commit):
    chain, contract, payload = case()
    pair(chain, replace(payload, **{field: value}), commit, 11)
    verdict, evidence = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('MISSING', 'reveal' if commit == 3 else 'commit')
    assert (evidence['commit'] is not None) == (commit == 3)
    assert evidence['reveal'] is None


def test_old_deposit_domain_cannot_establish_report_only_diagnostic(monkeypatch):
    chain, contract, payload = case()
    values = {k: v for k, v in payload.__dict__.items() if k != 'mining_netuid'}
    old = Submission(**values, deposit_block=1, deposit_event=0)
    pair(chain, old, 7, 11)
    verdict, _ = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('MISSING', 'commit')


@pytest.mark.parametrize('failure', ['unsuccessful', 'wrong_subnet', 'wrong_signer', 'bad_hash'])
def test_unrelated_or_failed_call_cannot_support_late_diagnostic(monkeypatch, failure):
    chain, contract, payload = case()
    raw = payload.encode()
    chain.submit(7, hashlib.sha256(raw).digest())
    kwargs = {'success': False} if failure == 'unsuccessful' else (
        {'netuid': 9} if failure == 'wrong_subnet' else {'signer': OTHER} if failure == 'wrong_signer' else {})
    chain.submit(11, replace(payload, salt=b'z' * 16).encode() if failure == 'bad_hash' else raw, **kwargs)
    verdict, _ = collect(chain, contract, monkeypatch)
    assert (verdict['status'], verdict['reason']) == ('MISSING', 'commit')


def test_optional_context_leaves_legacy_selection_unchanged():
    chain, contract, payload = case()
    raw = payload.encode()
    calls = [dict(block_number=n, extrinsic_index=1, signer=MINER.hex(),
        call='Commitments.set_commitment', data_hex=data.hex(), structurally_valid=True,
        successful=True, target_netuid=82, timestamp_ms=moment(n),
        genesis_hash=contract['genesis_hash']) for n, data in
        [(7, hashlib.sha256(raw).digest()), (11, raw)]]
    kwargs = dict(hotkey_hex=MINER.hex(), mining_netuid=82,
        **{k: contract[k] for k in ('commit_open_ms', 'commit_close_ms', 'reveal_open_ms', 'reveal_close_ms')})
    assert select_for_miner(calls, **kwargs)['commit'] is None
    legacy = {**contract, 'eligibility_policy_version': 'attributed-deposit-before-b-v1'}
    assert select_for_miner(calls, expected_round=legacy, **kwargs)['commit'] is None
    assert select_for_miner(calls, expected_round=contract, **kwargs)['commit']['block_number'] == 7
