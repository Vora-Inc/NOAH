"""ON startup validates isolated runtime and frozen binding before wallet use."""
import copy
import dataclasses
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from noah.core.calculation_record import encoded
from noah.core.report_publication import ReportPublicationRefused, check_worker_ready
from noah.core.report_runtime import ELIGIBILITY, startup_binding
from noah.core.settings import BadSetting, require_report_gate_runtime_ready
from noah.reports.tests.test_adjudication_contract import request


@pytest.fixture
def selected(tmp_path):
    policy = copy.deepcopy(request()['policy'])
    policy['eligibility_policy_version'] = ELIGIBILITY
    for contract in policy['required_rounds']:
        contract['eligibility_policy_version'] = ELIGIBILITY
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    return SimpleNamespace(report_gate_enabled=True, report_gate_policy=str(path),
                           publish_mode='direct', scoring_mode='legacy', netuid=82)


@pytest.mark.parametrize('mode', ['legacy', 'points-204040-v5'])
def test_supported_direct_modes_load_policy_and_validate_worker(selected, mode):
    selected.scoring_mode = mode
    with mock.patch('noah.core.report_publication.check_worker_ready') as worker:
        require_report_gate_runtime_ready(selected)
    worker.assert_called_once_with()


@pytest.mark.parametrize('mode', ['points-402040-v2', 'points-402040-v3', 'points-402040-v4'])
def test_unqualified_historical_modes_refuse_before_worker_or_policy(selected, mode):
    selected.scoring_mode = mode
    with mock.patch.object(Path, 'open', side_effect=AssertionError('policy inspected')):
        with pytest.raises(BadSetting, match='only Legacy, V2.0, or V6'):
            require_report_gate_runtime_ready(selected)


def test_missing_or_malformed_policy_refuses_before_worker(selected):
    with mock.patch('noah.core.report_publication.check_worker_ready') as worker:
        Path(selected.report_gate_policy).write_text('{not json')
        with pytest.raises(ReportPublicationRefused):
            require_report_gate_runtime_ready(selected)
        Path(selected.report_gate_policy).unlink()
        with pytest.raises(ReportPublicationRefused):
            require_report_gate_runtime_ready(selected)
        worker.assert_not_called()


def test_wrong_mining_subnet_refuses_before_worker(selected):
    selected.netuid = 1
    with mock.patch('noah.core.report_publication.check_worker_ready') as worker:
        with pytest.raises(BadSetting, match='mining subnet differs'):
            require_report_gate_runtime_ready(selected)
        worker.assert_not_called()


def test_binding_checks_are_read_only_and_refuse_wrong_network_and_cutoff(selected, tmp_path):
    scores = tmp_path / 'scores'
    startup_binding(selected, '0x' + '11' * 32, scores)
    assert not scores.exists()
    with pytest.raises(ReportPublicationRefused, match='network differs'):
        startup_binding(selected, '0x' + '22' * 32, scores)
    state = scores / 'report-state'
    state.mkdir(parents=True)
    marker = state / 'activation.json'
    original = encoded(dict(schema='noah-report-activation/1', genesis_hash='11' * 32,
                            mining_netuid=82, effective_day=1))
    marker.write_bytes(original)
    with pytest.raises(ReportPublicationRefused, match='activation cutoff'):
        startup_binding(selected, '0x' + '11' * 32, scores)
    assert marker.read_bytes() == original


@pytest.mark.parametrize('retained', ['report-state/day-0.request.json', 'reports/retained-package'])
def test_partial_report_state_without_boundary_refuses_read_only_startup(selected, tmp_path, retained):
    scores = tmp_path / 'scores'
    item = scores / retained
    item.parent.mkdir(parents=True)
    item.write_bytes(b'preserve original evidence')
    with pytest.raises(ReportPublicationRefused, match='without its activation boundary'):
        startup_binding(selected, '0x' + '11' * 32, scores)
    assert item.read_bytes() == b'preserve original evidence'
    assert not (scores / 'report-state/activation.json').exists()


def test_off_does_not_inspect_worker_policy_or_scores():
    settings = SimpleNamespace(report_gate_enabled=False)
    with mock.patch.object(Path, 'open', side_effect=AssertionError('read')), \
         mock.patch.object(Path, 'is_file', side_effect=AssertionError('stat')), \
         mock.patch.dict(os.environ, {}, clear=True), \
         mock.patch('subprocess.run', side_effect=AssertionError('worker')):
        require_report_gate_runtime_ready(settings)
        startup_binding(settings, object(), object())


def test_worker_must_be_explicit_and_primary_sdk_is_refused(monkeypatch):
    monkeypatch.delenv('NOAH_REPORT_PYTHON', raising=False)
    with pytest.raises(ReportPublicationRefused, match='explicit NOAH_REPORT_PYTHON'):
        check_worker_ready()
    monkeypatch.setenv('NOAH_REPORT_PYTHON', '/missing/report-python')
    with pytest.raises(ReportPublicationRefused, match='worker is missing'):
        check_worker_ready()
    monkeypatch.setenv('NOAH_REPORT_PYTHON', sys.executable)
    with pytest.raises(ReportPublicationRefused, match='isolation'):
        check_worker_ready()


def test_actual_isolated_worker_must_load_this_exact_source(monkeypatch):
    import noah.core.report_publication as publication
    worker = Path(__file__).resolve().parents[3] / '.venv-reports/bin/python'
    if not worker.is_file():
        pytest.skip('isolated worker absent; managed release has separate runtime checks')
    monkeypatch.setenv('NOAH_REPORT_PYTHON', str(worker))
    check_worker_ready()
    monkeypatch.setattr(publication, '__file__', '/different-release/noah/core/report_publication.py')
    with pytest.raises(ReportPublicationRefused, match='selected source differ'):
        check_worker_ready()


@pytest.mark.parametrize('argv', [[], ['--dry-run'], ['--starter-guard']])
def test_each_entry_surface_refuses_bad_binding_before_wallet(selected, tmp_path, argv):
    import neurons.validator as validator
    from neurons.tests.test_validator_entry import SETTINGS
    settings = dataclasses.replace(SETTINGS, report_gate_enabled=True,
        report_gate_policy=selected.report_gate_policy, publish_mode='direct', netuid=82)
    with mock.patch.object(validator, 'check_library_versions'), \
         mock.patch.object(validator, 'load_settings', return_value=settings), \
         mock.patch.object(validator, 'connect', return_value=(object(), '0x' + '22' * 32)), \
         mock.patch.object(validator, '_submitted_record_dir', return_value=tmp_path / 'scores'), \
         mock.patch.object(validator, '_load_wallet') as wallet, \
         mock.patch.object(validator, 'build_chain_adapter') as build:
        assert validator.main(argv) == 2
    wallet.assert_not_called()
    build.assert_not_called()
    assert not (tmp_path / 'scores').exists()


def test_valid_dry_run_checks_binding_without_creating_activation_or_loading_wallet(selected, tmp_path):
    import neurons.validator as validator
    from neurons.tests.test_validator_entry import SETTINGS
    settings = dataclasses.replace(SETTINGS, report_gate_enabled=True,
        report_gate_policy=selected.report_gate_policy, publish_mode='direct', netuid=82,
        expected_signer_address='public-dry-run-address')
    reader = mock.Mock()
    reader.current_block.return_value = 100
    with mock.patch.object(validator, 'check_library_versions'), \
         mock.patch.object(validator, 'load_settings', return_value=settings), \
         mock.patch.object(validator, 'connect', return_value=(object(), '0x' + '11' * 32)), \
         mock.patch.object(validator, '_submitted_record_dir', return_value=tmp_path / 'scores'), \
         mock.patch.object(validator, 'ChainReader', return_value=reader), \
         mock.patch.object(validator, 'gate', return_value=([('test', 'ok')], 3)), \
         mock.patch.object(validator, 'survey', return_value=True), \
         mock.patch.object(validator, '_load_wallet') as wallet, \
         mock.patch.object(validator, 'build_chain_adapter') as build:
        assert validator.main(['--dry-run']) == 0
    wallet.assert_not_called()
    build.assert_not_called()
    assert not (tmp_path / 'scores').exists()
