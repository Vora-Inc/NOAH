"""Behavioral boundary tests; all chain observations here are synthetic."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from noah.reports.verification import verify_shadow
from noah.reports.tests.sample import fixture



@pytest.fixture
def case():
    value = fixture()
    return value["round"], value["evidence"]


def status(case):
    return verify_shadow(*case)["status"]


def test_complete_history_without_deposit_is_ineligible(case):
    case[1]["deposit_history"]["receipts"] = []
    assert status(case) == "INELIGIBLE"


@pytest.mark.parametrize("state", ["INCOMPLETE", "FAILED"])
def test_uncollected_history_is_never_evidence_of_ineligibility(case, state):
    case[1]["deposit_history"].update(state=state, receipts=[])
    assert status(case) == "UNVERIFIABLE"


def test_older_one_rao_receipt_with_zero_credit_qualifies(case):
    assert status(case) == "VERIFIED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("from_block", 1),
        ("through_block", 98),
        ("gaps", [[20, 21]]),
        ("errors", ["RPC failure"]),
        ("genesis_hash", "99" * 32),
        ("eligibility_policy_version", "another-policy"),
        ("ledger_kind", "native"),
        ("round_block_hash", "99" * 32),
    ],
)
def test_history_certificate_must_cover_entire_required_window(case, field, value):
    case[1]["deposit_history"][field] = value
    case[1]["deposit_history"]["receipts"] = []
    assert status(case) == "UNVERIFIABLE"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
def test_actual_transaction_target_must_match_round_even_if_sidecar_agrees(case, leg):
    case[1][leg]["target_netuid"] = 64
    assert status(case) == "MALFORMED"


def test_unhashed_sidecar_is_not_an_authority(case):
    case[1]["sidecar"]["mining_netuid"] = 999
    assert status(case) == "VERIFIED"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
@pytest.mark.parametrize("field", ["finalized", "successful"])
def test_unproven_transaction_cannot_verify(case, leg, field):
    case[1][leg][field] = False
    assert status(case) == "UNVERIFIABLE"


def test_missing_commit_with_incomplete_collection_is_unverifiable(case):
    case[1]["commit"] = None
    case[1]["collection"]["state"] = "INCOMPLETE"
    assert status(case) == "UNVERIFIABLE"


def test_missing_commit_after_complete_collection_is_missing(case):
    case[1]["commit"] = None
    assert status(case) == "MISSING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("genesis_hash", "99" * 32),
        ("block_hash", "99" * 32),
        ("netuid", 51),
        ("calculation_version", "99" * 20),
        ("ark_coldkey", "99" * 32),
        ("status", "UNAVAILABLE"),
    ],
)
def test_wrong_reference_context_is_unverifiable(case, field, value):
    case[1]["calculation"][field] = value
    assert status(case) == "UNVERIFIABLE"


def test_reference_coverage_gap_is_unverifiable(case):
    case[1]["calculation"]["coverage"]["coverage_gap_exceeds_holder"] = True
    assert status(case) == "UNVERIFIABLE"


def test_hashed_reported_subnet_must_match_round_even_with_matching_hash(case):
    raw = bytearray.fromhex(case[1]["reveal"]["data_hex"])
    raw[107:109] = (51).to_bytes(2, "little")
    case[1]["reveal"]["data_hex"] = raw.hex()
    case[1]["commit"]["data_hex"] = hashlib.sha256(raw).hexdigest()
    assert status(case) == "MALFORMED"


@pytest.mark.parametrize(
    "leg,field", [("commit", "commit_close_ms"), ("reveal", "reveal_close_ms")]
)
def test_deadline_is_exclusive(case, leg, field):
    case[1][leg]["timestamp_ms"] = case[0][field]
    assert status(case) == "LATE"


def test_wrong_number_is_mismatch(case):
    case[1]["calculation"]["output1"]["drop_ppm"] = 179
    assert status(case) == "MISMATCH"


def test_malformed_reference_number_is_not_miner_fault(case):
    case[1]["calculation"]["output1"]["drop_ppm"] = "178"
    assert status(case) == "UNVERIFIABLE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("destination_coldkey", "99" * 32),
        ("destination_hotkey", "99" * 32),
        ("owner_at_deposit", "99" * 32),
        ("origin_hotkey", "99" * 32),
        ("token_netuid", 51),
        ("amount_rao", 0),
        ("unattributed_reason", "unattributed"),
    ],
)
def test_attribution_is_required_even_when_receipt_exists(case, field, value):
    case[1]["deposit_history"]["receipts"][0][field] = value
    assert status(case) == "INELIGIBLE"


def test_round_cannot_relabel_actual_commitment_subnet(case):
    case[0]["mining_netuid"] = 64
    assert status(case) == "MALFORMED"


def test_no_rewards_and_inputs_untouched(case):
    before = copy.deepcopy(case)
    result = verify_shadow(*case)
    assert result["status"] == "VERIFIED"
    assert set(result) == {"schema", "mode", "status", "reason", "round_sha256", "evidence_sha256"}
    assert result["mode"] == "SHADOW"
    assert before == case


@pytest.mark.parametrize("field", ["destination_coldkey", "amount_rao", "owner_at_deposit"])
def test_incomplete_receipt_is_not_a_proven_eligibility_failure(case, field):
    del case[1]["deposit_history"]["receipts"][0][field]
    assert status(case) == "UNVERIFIABLE"


def test_hash_must_match_exact_reveal(case):
    case[1]["commit"]["data_hex"] = "00" * 32
    assert status(case) == "MALFORMED"


def test_truncated_payload_is_malformed(case):
    case[1]["reveal"]["data_hex"] = case[1]["reveal"]["data_hex"][:-2]
    assert status(case) == "MALFORMED"


def test_native_sn82_uses_native_history(case):
    case[0]["reported_netuid"] = 82
    case[1]["calculation"]["netuid"] = 82
    case[1]["deposit_history"]["ledger_kind"] = "native"
    case[1]["deposit_history"]["receipts"][0]["token_netuid"] = 82
    raw = bytearray.fromhex(case[1]["reveal"]["data_hex"])
    raw[107:109] = (82).to_bytes(2, "little")
    case[1]["reveal"]["data_hex"] = raw.hex()
    case[1]["commit"]["data_hex"] = hashlib.sha256(raw).hexdigest()
    assert status(case) == "VERIFIED"


def test_ambiguous_receipt_is_unverifiable(case):
    rows = case[1]["deposit_history"]["receipts"]
    rows.append(copy.deepcopy(rows[0]))
    assert status(case) == "UNVERIFIABLE"


def test_receipt_at_b_is_not_eligible(case):
    case[1]["deposit_history"]["receipts"][0]["block"] = 100
    raw = bytearray.fromhex(case[1]["reveal"]["data_hex"])
    raw[149:153] = (100).to_bytes(4, "little")
    case[1]["reveal"]["data_hex"] = raw.hex()
    case[1]["commit"]["data_hex"] = hashlib.sha256(raw).hexdigest()
    assert status(case) == "INELIGIBLE"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
def test_missing_collected_transaction_body_is_unverifiable(case, leg):
    del case[1][leg]["data_hex"]
    assert status(case) == "UNVERIFIABLE"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
@pytest.mark.parametrize("field", ["target_netuid", "call", "signer"])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_transaction_identity_is_not_a_miner_fault(case, leg, field, missing):
    if missing:
        del case[1][leg][field]
    else:
        case[1][leg][field] = None
    assert status(case) == "UNVERIFIABLE"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
@pytest.mark.parametrize(
    "field,value", [("target_netuid", "82"), ("call", []), ("signer", "not-an-account")]
)
def test_unusable_transaction_identity_is_unverifiable(case, leg, field, value):
    case[1][leg][field] = value
    assert status(case) == "UNVERIFIABLE"


@pytest.mark.parametrize("leg", ["commit", "reveal"])
@pytest.mark.parametrize("field,value", [("call", "System.remark"), ("signer", "99" * 32)])
def test_proven_wrong_call_or_signer_remains_a_miner_fault(case, leg, field, value):
    case[1][leg][field] = value
    assert status(case) == "MALFORMED"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("location", ["calculation", "extra"])
def test_nonfinite_evidence_returns_unverifiable_without_a_fabricated_hash(case, value, location):
    valid = copy.deepcopy(case)
    if location == "calculation":
        case[1]["calculation"]["output1"]["drop_ppm"] = value
    else:
        case[1]["extra"] = value
    result = verify_shadow(*case)
    assert result["status"] == "UNVERIFIABLE"
    assert result["evidence_sha256"] is None
    assert len(result["round_sha256"]) == 64
    json.dumps(result, allow_nan=False)
    assert status(valid) == "VERIFIED"


@pytest.mark.parametrize("cyclic", [False, True])
def test_nonserializable_evidence_cannot_abort_verification(case, cyclic):
    case[1]["extra"] = case[1] if cyclic else {1, 2}
    result = verify_shadow(*case)
    assert result["status"] == "UNVERIFIABLE"
    assert result["evidence_sha256"] is None


def test_cli_handles_nonfinite_reference_without_a_traceback(case, tmp_path):
    case[1]["calculation"]["output1"]["drop_ppm"] = float("nan")
    fixture = tmp_path / "nonfinite.json"
    fixture.write_text(json.dumps({"round": case[0], "evidence": case[1]}))
    completed = subprocess.run(
        [sys.executable, "-m", "noah.reports.verification", "--fixture", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "UNVERIFIABLE"
    assert result["evidence_sha256"] is None


@pytest.mark.parametrize("mining,reported", [(82, 64), (82, 82), (2, 3), (2, 2)])
def test_round_bound_mining_subnet_and_native_history(case, mining, reported):
    contract, evidence = case
    contract.update(mining_netuid=mining, reported_netuid=reported)
    for leg in ("commit", "reveal"):
        evidence[leg]["target_netuid"] = mining
    evidence["calculation"]["netuid"] = reported
    evidence["deposit_history"]["ledger_kind"] = "native" if mining == reported else "foreign"
    evidence["deposit_history"]["receipts"][0]["token_netuid"] = reported
    payload = bytearray.fromhex(evidence["reveal"]["data_hex"])
    payload[107:109] = reported.to_bytes(2, "little")
    evidence["reveal"]["data_hex"] = payload.hex()
    evidence["commit"]["data_hex"] = hashlib.sha256(payload).hexdigest()
    assert status(case) == "VERIFIED"
    evidence["commit"]["target_netuid"] = mining + 1
    assert status(case) == "MALFORMED"


@pytest.mark.parametrize("field", ["mining_netuid", "reported_netuid"])
@pytest.mark.parametrize("value", [0, -1, 65536, True, "2"])
def test_round_rejects_invalid_subnet_identity(case, field, value):
    from noah.reports.verification import validate_round
    case[0][field] = value
    with pytest.raises(ValueError):
        validate_round(case[0])


@pytest.mark.parametrize('submission', ['missing_commit', 'missing_reveal', 'malformed'])
@pytest.mark.parametrize('failure', ['engine', 'threshold', 'coverage'])
def test_unavailable_calculation_never_penalizes_a_submission(case, submission, failure):
    if submission == 'missing_commit':
        case[1]['commit'] = None
    elif submission == 'missing_reveal':
        case[1]['reveal'] = None
    else:
        case[1]['commit']['data_hex'] = '00' * 32
    # A usable calculation still permits ordinary protocol failures.
    assert status(case) in {'MISSING', 'MALFORMED'}
    if failure == 'engine':
        case[1]['calculation']['status'] = 'UNAVAILABLE'
    elif failure == 'threshold':
        case[1]['calculation']['output2'] = {'status': 'UNAVAILABLE', 'reason': 'threshold_not_reached'}
    else:
        case[1]['calculation']['coverage']['coverage_gap_exceeds_holder'] = True
    assert status(case) == 'UNVERIFIABLE'
