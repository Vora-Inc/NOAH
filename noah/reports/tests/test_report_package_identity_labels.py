"""Actual synthetic provider output, strict schemas, and unchanged secret scans."""
import copy
import json

import pytest

from noah.secret_scan import _report_identity_labels, scan_text
from noah.reports.tests.test_raw_export_integration import build
from noah.reports.identity_shapes import (validate_adjudication, validate_roster,
    validate_required_roster, validate_verdicts, validate_engine, validate_collector)

VALIDATORS = {
    'noah.report-adjudication-manifest/1': validate_adjudication,
    'noah.report-adjudication-result/1': validate_adjudication,
    'noah.report-snapshot-roster/1': validate_roster,
    'noah.report-required-roster/1': validate_required_roster,
    'noah.report-final-verdicts/1': validate_verdicts,
    'bfpoc-calc-v1': validate_engine,
}

# Build the deliberate scanner canary at runtime so the release-source scan
# does not mistake this test for a literal private-key header. No key follows;
# the tested scanner receives the identical complete marker in both cases.
CANARY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"


def encode(value, style='compact'):
    if style == 'pretty':
        return json.dumps(value, sort_keys=True, indent=2) + '\n'
    if style == 'spaced':
        return json.dumps(value, sort_keys=True)
    return json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n'


@pytest.fixture(scope='module')
def artifacts(tmp_path_factory):
    fixture = build(tmp_path_factory.mktemp('identity-package') / 'synthetic')
    found = {}
    for path in fixture.args.package.rglob('*.json'):
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            continue
        schema = value.get('schema')
        if schema in VALIDATORS:
            found[schema] = value
        elif set(value) == {'evidence', 'observation'}:
            found['collector'] = value
    found[fixture.result['schema']] = fixture.result
    assert len(found) == 7
    return found


@pytest.mark.parametrize('style', ['compact', 'pretty', 'spaced'])
def test_generated_artifacts_preserve_values_and_offsets(artifacts, style):
    for schema, value in artifacts.items():
        validator = validate_collector if schema == 'collector' else VALIDATORS[schema]
        nodes = validator(value)
        text = encode(value, style)
        projected = _report_identity_labels(text)
        assert len(projected) == len(text) and projected != text, schema
        # Only the exact identified label characters can change.
        assert all(left == right or right == 'p' for left, right in zip(text, projected))
        assert [index for index, ch in enumerate(text) if ch == '\n'] == [index for index, ch in enumerate(projected) if ch == '\n']
        for owner, names in nodes:
            assert all(owner[name] in projected for name in names)
        assert not scan_text(text), schema


@pytest.mark.parametrize('damage', ['field', 'hash', 'type', 'schema'])
def test_unknown_or_malformed_enclosing_shapes_keep_identity_labels(artifacts, damage):
    for schema, original in artifacts.items():
        value = copy.deepcopy(original)
        validator = validate_collector if schema == 'collector' else VALIDATORS[schema]
        nodes = validator(value)
        owner, names = nodes[0]
        if damage == 'field':
            owner['unexpected'] = True
        elif damage == 'hash':
            owner[next(iter(names))] = 'zz' * 32
        elif damage == 'type':
            owner[next(iter(names))] = 123
        elif schema == 'collector':
            value['observation']['unknown'] = True
        else:
            value['schema'] = 'unknown.report/1'
        with pytest.raises((ValueError, TypeError, KeyError)):
            validator(value)
        projected = _report_identity_labels(encode(value))
        # Nested independent schemas can still validate; the damaged owner cannot.
        assert any('"' + name + '":' in projected for name in names), schema


@pytest.mark.parametrize('damage', ['duplicate', 'whitespace', 'nan'])
def test_noncanonical_or_ambiguous_json_is_not_projected(artifacts, damage):
    for value in artifacts.values():
        text = encode(value)
        if damage == 'duplicate':
            text = text.replace('{', '{"hotkey":"' + 'ab' * 32 + '","hotkey":"' + 'cd' * 32 + '",', 1)
        elif damage == 'whitespace':
            text = ' ' + text
        else:
            text = text.replace('{', '{"unknown":NaN,', 1)
        assert _report_identity_labels(text) == text


@pytest.mark.parametrize('secret', [
    {'secret_seed': 'ab' * 32},
    {'unknown': {'hotkey': 'cd' * 32}},
    {'private': CANARY_MARKER},
])
def test_valid_artifacts_do_not_hide_sibling_credentials(artifacts, secret):
    for value in artifacts.values():
        assert scan_text(encode({'artifact': value, **secret}))


def test_credentials_in_valid_free_text_still_scan(artifacts):
    value = copy.deepcopy(artifacts['collector'])
    row = next(iter(value['evidence'].values()))
    row['collection']['errors'] = [CANARY_MARKER]
    assert validate_collector(value)
    assert any(finding.kind == 'private-key-header' for finding in scan_text(encode(value)))


def test_synthetic_package_has_real_retained_commit_and_reveal(artifacts):
    envelope = artifacts['collector']
    for row in envelope['evidence'].values():
        for phase in ('commit', 'reveal'):
            assert row[phase] is not None
            assert row[phase]['call'] == 'Commitments.set_commitment'
            assert row[phase]['structurally_valid'] and row[phase]['successful']
            assert row['finality'][phase]['matches']
            assert row['candidates'][phase + 's']
    assert artifacts['noah.report-adjudication-result/1']['scored_decisions'][0]['outcome'] == 'verified'
    assert not scan_text(encode(envelope, 'spaced'))


@pytest.mark.parametrize('variant', ['wrong_report', 'unavailable_output', 'no_holder'])
def test_real_producer_failure_paths_remain_scannable(tmp_path, monkeypatch, variant):
    from noah.reports.tests import test_raw_export_integration as integration
    from noah.reports.tests.raw_export_fixture import RawChain
    from noah.reports.backing.scale import SimSwap
    from noah.reports.backing.round_config import calculation_for_shadow

    def chain(*, context=False, unassigned=False, assigned_missing=False):
        # Preserve the actual fixture API and its synthetic commit/reveal bytes.
        source = RawChain(context=context, unassigned=unassigned, assigned_missing=assigned_missing)
        if variant == 'wrong_report':
            source.engine.sim = lambda amount: SimSwap(amount // 11, amount, 0, 0, 0, 0)
        elif variant == 'unavailable_output':
            source.engine.sim = lambda amount: SimSwap(0, 0, 0, 0, 0, 0)
        else:
            source.engine.alpha_keys.clear()
        return source

    monkeypatch.setattr(integration, 'RawChain', chain)
    fixture = integration.build(tmp_path / variant)
    decision = fixture.result['scored_decisions'][0]
    assert decision['outcome'] == ('confirmed_failure' if variant == 'wrong_report' else 'unresolved')
    for folder in fixture.args.package.glob('rounds/*/reader_*/collection'):
        envelope = json.loads((folder / 'evidence.json').read_text())
        engine = json.loads((folder / 'engine.json').read_text())
        validate_engine(engine)
        validate_collector(envelope)
        assert not scan_text((folder / 'engine.json').read_text())
        assert not scan_text((folder / 'evidence.json').read_text())
        row = next(iter(envelope['evidence'].values()))
        assert row['commit'] is not None and row['reveal'] is not None
        if variant != 'wrong_report':
            assert row['calculation']['status'] == 'UNAVAILABLE'
            assert row['calculation']['output1'] == {'status': 'UNAVAILABLE', 'drop_ppm': 0}
            if variant == 'unavailable_output':
                assert engine['status'] == 'OK' and engine['output1']['status'] == 'UNAVAILABLE'
                contract = fixture.request['policy']['required_rounds'][0]
                assert calculation_for_shadow(contract, engine, allow_unavailable_outputs=True) is None
            else:
                assert engine['status'] == 'UNAVAILABLE' and engine['reason'] == 'no_holder'
            for damage in ('null', 'invented', 'field'):
                bad = copy.deepcopy(envelope)
                calculation = next(iter(bad['evidence'].values()))['calculation']
                if damage == 'field':
                    calculation['secret_seed'] = 'ab' * 32
                else:
                    calculation['output1']['drop_ppm'] = None if damage == 'null' else 1
                with pytest.raises(ValueError):
                    validate_collector(bad)
                assert scan_text(encode(bad))
    assert not scan_text((fixture.args.package / 'manifest.json').read_text())
    assert not scan_text(encode(fixture.result))
    for path in fixture.args.package.glob('rounds/*/final-verdicts.json'):
        assert not scan_text(path.read_text())


@pytest.mark.parametrize('variant', ['unsupported', 'rpc_failure'])
def test_gated_engine_exact_shapes_without_identity_exemptions(variant):
    from noah.reports.tests.test_engine_arithmetic import _chain, BLOCK, NETUID
    from noah.reports.backing.engine import _compute as compute
    source = _chain()
    if variant == 'unsupported':
        source.spec = 0
    else:
        source.fail = 'chain_getHeader'
    engine, _ = compute(source, NETUID, BLOCK)
    assert engine['status'] == ('UNSUPPORTED' if variant == 'unsupported' else 'UNAVAILABLE')
    assert validate_engine(engine) == []
    assert not scan_text(encode(engine))
    engine['ark_coldkey'] = '0x' + 'ab' * 32
    with pytest.raises(ValueError):
        validate_engine(engine)
    assert scan_text(encode(engine))


def test_calculation_hash_cannot_exist_without_engine_hash(artifacts):
    value = copy.deepcopy(artifacts['noah.report-adjudication-result/1'])
    assert value['rounds'][0]['calculation_sha256'] is not None
    value['rounds'][0]['engine_sha256'] = None
    with pytest.raises(ValueError, match='calculation binding'):
        validate_adjudication(value)
    assert scan_text(encode(value))
