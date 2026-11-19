"""Report-only policy: independent of deposits, with versioned wire identity."""
import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest

from noah.reports import miner
from noah.reports.backing.protocol.preimage import (
    REPORT_DOMAIN, REPORT_POLICY, REPORT_FORMAT, ReportSubmission, decode,
)
from noah.reports.backing.prepare import load_prepared, PrepareError
from noah.reports.common import ReportError, state_directory
from noah.reports.backing.submit import SubmitError
from noah.reports.tests.sample import fixture
from noah.reports.tests.test_miner_commands import case, sign, no_sign
from noah.reports.verification import verify_shadow


def report_case():
    value = fixture()
    r, e = value['round'], value['evidence']
    old = decode(bytes.fromhex(e['reveal']['data_hex']))
    fields = asdict(old)
    del fields['deposit_block'], fields['deposit_event']
    submission = ReportSubmission(**fields, mining_netuid=r['mining_netuid'])
    r['eligibility_policy_version'] = REPORT_POLICY
    set_payload(e, submission)
    return r, e, submission


def set_payload(evidence, submission):
    raw = submission.encode()
    evidence['reveal']['data_hex'] = raw.hex()
    evidence['commit']['data_hex'] = hashlib.sha256(raw).hexdigest()


def test_wire_version_omits_deposit_and_authenticates_both_subnets():
    _, _, submission = report_case()
    raw = submission.encode()
    assert len(raw) == REPORT_FORMAT.size == 203
    assert raw[:11] == REPORT_DOMAIN
    assert raw[107:111] == bytes.fromhex('40005200')  # reported64, mining82
    assert decode(raw) == submission
    assert not hasattr(decode(raw), 'deposit_block')
    assert not hasattr(decode(raw), 'deposit_event')
    assert hashlib.sha256(replace(submission, mining_netuid=83).encode()).digest() != hashlib.sha256(raw).digest()


@pytest.mark.parametrize('history', [None, {}, {'state': 'INCOMPLETE'}, {'receipts': 'invalid'}])
def test_deposit_evidence_does_not_affect_report_accuracy(history):
    r, e, _ = report_case()
    e['deposit_history'] = history
    assert verify_shadow(r, e)['status'] == 'VERIFIED'


def test_report_only_still_rejects_wrong_answers_and_wrong_mining_subnet():
    r, e, submission = report_case()
    set_payload(e, replace(submission, out1_ppm=submission.out1_ppm + 1))
    assert verify_shadow(r, e)['status'] == 'MISMATCH'
    set_payload(e, replace(submission, mining_netuid=83))
    assert verify_shadow(r, e)['reason'] == 'payload_context'


def test_old_and_new_policies_cannot_reinterpret_each_others_payload():
    r, e, _ = report_case()
    r['eligibility_policy_version'] = 'attributed-deposit-before-b-v1'
    assert verify_shadow(r, e)['status'] == 'MALFORMED'
    old = fixture()
    old['round']['eligibility_policy_version'] = REPORT_POLICY
    assert verify_shadow(old['round'], old['evidence'])['status'] == 'MALFORMED'


def test_collector_selects_new_report_reveal():
    from noah.reports.backing.select import select_for_miner
    r, e, _ = report_case()
    calls = [{**e[leg], 'structurally_valid': True} for leg in ('commit', 'reveal')]
    selected = select_for_miner(calls, hotkey_hex=e['snapshot']['hotkey'],
        mining_netuid=r['mining_netuid'], **{key: r[key] for key in (
            'commit_open_ms', 'commit_close_ms', 'reveal_open_ms', 'reveal_close_ms')})
    assert selected['commit'] == calls[0]
    assert selected['reveal'] == calls[1]


def test_new_miner_prepares_without_deposit_then_restarts_and_completes(case):
    c, r, chain, engine = case
    r['eligibility_policy_version'] = REPORT_POLICY
    result = miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    assert result['report_policy'] == REPORT_POLICY
    path = state_directory(c, r) / 'prepared.json'
    before = path.read_bytes()
    prepared = load_prepared(path)
    assert isinstance(prepared.submission, ReportSubmission)
    assert prepared.submission.mining_netuid == 82
    miner.prepare_report(c, r, compute_fn=no_sign)
    assert path.read_bytes() == before
    assert miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=sign)['sent']
    chain.now = r['commit_close_ms'] + 1
    assert miner.submit_tick(c, r, chain=chain, signer_factory=no_sign)['action'] == 'waiting'
    chain.now = r['reveal_open_ms']
    assert miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=sign)['sent']
    assert miner.submit_tick(c, r, chain=chain, signer_factory=no_sign)['action'] == 'complete'
    assert [len(tx['payload']) for tx in chain.sent] == [32, 203]
    assert path.read_bytes() == before


def test_report_only_refuses_deposit_flags_before_creating_state(case):
    c, r, _, _ = case
    r['eligibility_policy_version'] = REPORT_POLICY
    with pytest.raises(ReportError, match='deposit references'):
        miner.prepare_report(c, r, 90, 3, compute_fn=no_sign)
    assert not (Path(c.state_dir) / 'reports').exists()


def test_sidecar_cannot_change_committed_mining_subnet(case):
    c, r, _, engine = case
    r['eligibility_policy_version'] = REPORT_POLICY
    miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    path = state_directory(c, r) / 'prepared.json'
    saved = json.loads(path.read_text())
    saved['mining_netuid'] = 83
    path.write_text(json.dumps(saved))
    with pytest.raises(PrepareError, match='committed subnet'):
        load_prepared(path)


def test_production_submission_is_default_off_and_explicitly_testable_without_network(case):
    from noah.reports.backing.protocol.policy import MAINNET_GENESIS
    c, r, chain, engine = case
    r['eligibility_policy_version'] = REPORT_POLICY
    r['genesis_hash'] = MAINNET_GENESIS.removeprefix('0x')
    c.genesis_hash = r['genesis_hash']
    chain.genesis_hash = MAINNET_GENESIS
    miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    with pytest.raises(SubmitError, match='explicit'):
        miner.submit_tick(c, r, chain=chain, authorized=True, signer_factory=no_sign)
    assert not chain.sent
    assert miner.submit_tick(c, r, chain=chain, authorized=True,
                             allow_mainnet=True, signer_factory=sign)['sent']
    assert len(chain.sent) == 1  # Synthetic FakeChain; no real network or key.


def test_mainnet_opt_in_never_overrides_network_binding(case):
    c, r, chain, engine = case
    r['eligibility_policy_version'] = REPORT_POLICY
    miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    chain.genesis_hash = '0x' + 'ee' * 32
    with pytest.raises(SubmitError, match='does not match'):
        miner.submit_tick(c, r, chain=chain, authorized=True, allow_mainnet=True, signer_factory=no_sign)
    assert not chain.sent


def test_report_collection_never_visits_pre_snapshot_history(tmp_path, monkeypatch):
    from noah.reports.tests.test_history import SyntheticChain, _round, _run
    chain = SyntheticChain(10)
    r = _round(chain, 6)
    r['eligibility_policy_version'] = REPORT_POLICY
    def forbidden(*args, **kwargs):
        raise AssertionError('report must not call deposit history')
    monkeypatch.setattr('noah.reports.backing.collector._history', forbidden)
    result = _run(chain, r, monkeypatch, out_dir=tmp_path)
    assert min(chain.header_numbers) == 6
    root = Path(result['directory'])
    assert not (root / 'history-checkpoint.json').exists()
    assert all(e['deposit_history'] == {'state': 'NOT_APPLICABLE'} for e in result['evidence'].values())
    assert all(e['collection']['state'] == 'COMPLETE' for e in result['evidence'].values())


def test_report_window_gap_and_wrong_snapshot_cannot_be_complete():
    from noah.reports.backing.collector import report_window_span, _walk
    from noah.reports.tests.test_history import SyntheticChain, _round, _hash
    chain = SyntheticChain(10)
    r = _round(chain, 6)
    rows, errors = _walk(chain, chain.head, stop={'hash': _hash(6), 'number': 6}, max_blocks=4)
    assert report_window_span(rows, r, errors, _hash(0))['time_complete']
    assert not report_window_span(rows[1:], r, errors, _hash(0))['time_complete']
    assert not report_window_span(rows, r, errors, '0x' + 'ff' * 32)['time_complete']
    with pytest.raises(ValueError, match='block budget'):
        _walk(chain, chain.head, stop={'hash': _hash(6), 'number': 6}, max_blocks=3)
    rows[1]['timestamp_ms'] = rows[0]['timestamp_ms']
    assert not report_window_span(rows, r, errors, _hash(0))['time_complete']


def test_bare_hex_hotkey_starting_five_is_not_misread_as_ss58(monkeypatch):
    from noah.reports.backing import collector
    from noah.reports.tests.test_history import SyntheticChain, _round, _engine
    chain = SyntheticChain(10)
    contract = _round(chain, 6)
    contract['eligibility_policy_version'] = REPORT_POLICY
    monkeypatch.setattr(collector, 'compute', lambda *_args, **_kwargs: (_engine(contract), {}))
    result = collector._collect(chain, 'synthetic', [], contract, None, ['5' * 64],
                                lambda: 0, chain_source=object())
    assert '5' * 64 in result['evidence']
    assert result['evidence']['5' * 64]['snapshot']['hotkey'] == '5' * 64
