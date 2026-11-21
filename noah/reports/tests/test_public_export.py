"""Synthetic exporter contracts, NOT Mainnet or paired-chain proof.

The injected verifier models an authenticated result boundary. Separate producer
integration must exercise the real default offline adjudicator on raw packages.
The cross-language test runs these actual exporter bytes through the website CLI.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from noah.reports import public_export as exporter


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def artifact_digest(value):
    return hashlib.sha256(canonical(value) + b"\n").hexdigest()


def competing_correction(directory, package, report, raw, now_ms, ready, start, result):
    """Independent processes share only the immutable output history and lock."""
    ready.put(True)
    if not start.wait(10):
        result.put("timeout")
        return
    try:
        exporter._write_export(directory, package, report, raw, now_ms=now_ms)
        result.put("created")
    except exporter.PublicExportError:
        result.put("refused")


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="noah-public-export-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "private-package"
        self.package.mkdir()
        # Deliberately misleading saved content: only verifier results may be used.
        (self.package / "manifest.json").write_text('{"status":"COMPLETE","forged":true}')
        self.output = self.root / "public"
        self.receipt_path = self.root / "activation.json"
        self.published_at = "2026-11-15T01:00:00Z"
        self.now_ms = 1794787200000
        self.manifest_sha = "9" * 64
        self.contract = {
            "schema": "noah.backing.round.v1", "genesis_hash": exporter.GENESIS,
            "mining_netuid": 82, "reported_netuid": 7, "round_date": "2026-11-15",
            "block_number": 7000000, "block_hash": "2" * 64,
            "block_timestamp_ms": 1794700800000, "commit_open_ms": 1794700800000,
            "commit_close_ms": 1794701400000, "reveal_open_ms": 1794701400000,
            "reveal_close_ms": 1794702600000, "eligibility_policy_version": exporter.POLICY,
            "scoring_release": "points-v20", "release_prefix": "1" * 32,
            "calculation_version": "76f629a3933967170229a732c4b02740be22e5eb",
            "ark_coldkey": "a" * 64, "ark_validator_hotkey": "b" * 64,
        }
        self.members = [dict(uid=i, hotkey=str(i + 3) * 64, coldkey=str(i + 5) * 64,
                             registration_block=6999000 + i) for i in range(2)]
        day = self.contract["block_timestamp_ms"] // 86400000
        evaluation = {"block_number": 7000200, "block_hash": "8" * 64, "timestamp_ms": 1794702700000}
        self.request = {
            "schema": "noah.report-adjudication-request/1",
            "policy": {"schema": "noah.report-policy/1", "effective_day": day,
                "genesis_hash": exporter.GENESIS, "mining_netuid": 82,
                "eligibility_policy_version": exporter.POLICY, "required_rounds": [self.contract]},
            "genesis_hash": exporter.GENESIS, "mining_netuid": 82, "eval_day": day,
            "day_anchor_ts": 0, "evaluation": evaluation,
            "calculation": {"reference": {"path": "evidence/" + "7" * 64 + ".json",
                "sha256": "7" * 64, "day": day, "genesis_hash": "0x" + exporter.GENESIS,
                "candidate_sha256": None}, "fixture_sha256": "6" * 64},
            # Only one current scored seat: both required-at-B members must count.
            "scored_seats": [{**self.members[0], "registration_day": day - 1,
                              "observed_at": {"block_number": 7000200, "block_hash": "8" * 64}}],
        }
        self.engine = {
            "schema": "bfpoc-calc-v1", "block_number": 7000000, "block_hash": "0x" + "2" * 64,
            "netuid": 7, "ark_coldkey": "0x" + "a" * 64, "spec_version": 490,
            "status": "OK", "reason": None,
            "output1": {"status": "OK", "reason": None, "drop_ppm": 123456},
            "output2": {"status": "OK", "reason": None, "alpha_required_rao": 9007199255000000},
            "output3": {"status": "OK", "reason": None, "alpha_required_rao": 18446744073709000000},
            "private_canary": "https://private.invalid/SECRET_EXPORT_CANARY",
            "holder": {"ss58": "PRIVATE_HOLDER_CANARY", "available_rao": 999999999},
            "coverage": {"coverage_gap_exceeds_holder": False},
        }
        self.required = {"schema": "noah.report-required-roster/1", "evaluation": evaluation,
            "snapshot": {"schema": "noah.report-snapshot-roster/1", "genesis_hash": exporter.GENESIS,
                "mining_netuid": 82, "block_number": 7000000, "block_hash": "2" * 64,
                "timestamp_ms": 1794700800000, "spec_version": 490, "members": deepcopy(self.members)},
            "required_members": deepcopy(self.members)}
        self.verdicts = {"schema": "noah.report-final-verdicts/1", "evaluation": evaluation,
            "rows": [{**row, "effect": "preserve", "outcome": "verified", "status": "VERIFIED", "reason": "exact_match",
                "sources": {label: {"verdict_sha256": "d" * 64, "evidence_sha256": "e" * 64}
                            for label in ("reader_a", "reader_b")}} for row in self.members]}
        self.artifact = {"contract": self.contract, "required_roster": self.required,
                         "engine": self.engine, "verdicts": self.verdicts, "evaluation": evaluation}
        self.summary = {"evaluation": evaluation, "resolved": True,
                        "readers": {"reader_a": "f" * 64, "reader_b": "0" * 64},
                        "calculation_sha256": "1" * 64}
        self.result = {"schema": "noah.report-adjudication-result/1", "manifest_sha256": self.manifest_sha,
            "status": "COMPLETE", "rounds": [self.summary], "round_artifacts": [self.artifact], "scored_decisions": []}
        self.receipt = {"schema": exporter.ACTIVATION_SCHEMA, "genesis_hash": exporter.GENESIS,
            "mining_netuid": 82, "eligibility_policy_version": exporter.POLICY,
            "effective_day": day, "approved_at": "2026-11-14T23:00:00Z",
            "report_enforcement": True, "publication_approved": True,
            "approval_reference": "SYNTHETIC UNIT APPROVAL, NOT A CHAIN ACTIVATION"}
        self.calls = []
        self.refresh_bindings()

    def refresh_bindings(self):
        self.round_sha = digest(self.contract)
        self.request["policy_sha256"] = digest(self.request["policy"])
        request_sha = digest(self.request)
        for envelope in (self.required, self.verdicts):
            envelope.update(request_sha256=request_sha, round_sha256=self.round_sha)
        self.verdicts.update(required_roster_sha256=artifact_digest(self.required),
                             engine_sha256=artifact_digest(self.engine))
        self.summary.update(round_sha256=self.round_sha, engine_sha256=artifact_digest(self.engine),
            required_roster_sha256=artifact_digest(self.required), verdicts_sha256=artifact_digest(self.verdicts))
        self.result.update(request_sha256=request_sha, policy_sha256=self.request["policy_sha256"])
        self.receipt["policy_sha256"] = self.request["policy_sha256"]
        self.write_receipt()

    def write_receipt(self):
        raw = canonical(self.receipt) + b"\n"
        self.receipt_path.write_bytes(raw)
        self.receipt_sha = hashlib.sha256(raw).hexdigest()

    def verify(self, package, request, manifest_sha256, *, include_round_artifacts):
        self.calls.append((package, deepcopy(request), manifest_sha256, include_round_artifacts))
        return deepcopy(self.result)

    def export(self, **overrides):
        kwargs = dict(round_sha256=self.round_sha, activation_receipt=self.receipt_path,
            activation_receipt_sha256=self.receipt_sha, published_at=self.published_at,
            out_dir=self.output, verifier=self.verify, now_ms=self.now_ms)
        kwargs.update(overrides)
        return exporter.export_public_round(self.package, self.request, self.manifest_sha, **kwargs)

    def test_export_replays_and_counts_authenticated_roster_not_current_scored_subset(self):
        result = self.export()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], (self.package, self.request, self.manifest_sha, True))
        self.assertEqual(result["report"]["verification"]["required_submissions"], 2)
        self.assertEqual(result["report"]["verification"]["counts"]["VERIFIED"], 2)
        self.assertTrue(result["created"])
        self.assertEqual(Path(result["path"]).read_bytes(), result["json_bytes"])

    def test_projection_allowlist_and_exact_hash_domains(self):
        report = self.export(out_dir=None)["report"]
        public = json.dumps(report)
        for private in ("SECRET_EXPORT_CANARY", "PRIVATE_HOLDER_CANARY", self.members[0]["hotkey"],
                        self.contract["ark_coldkey"], "approval_reference", "deposit_block"):
            self.assertNotIn(private, public)
        self.assertEqual(report["calculation"]["engine_sha256"], artifact_digest(self.engine))
        self.assertNotEqual(report["calculation"]["engine_sha256"], digest(self.engine))
        self.assertEqual(report["verification"]["verdicts_sha256"], artifact_digest(self.verdicts))
        body = {key: value for key, value in report.items() if key != "report_id"}
        self.assertEqual(report["report_id"], digest(body))
        self.assertNotEqual(report["report_id"], artifact_digest(body))
        self.assertEqual(report["calculation"]["output3"]["alpha_required_rao"], "18446744073709000000")

    def assign_first(self):
        self.contract["commitment_format"] = "context-v1"
        self.request["assignments"] = [{**self.members[0], "targets": [7]}]
        self.refresh_bindings()

    def test_assignment_counts_exclude_unassigned_failures_but_keep_full_proof_hash(self):
        self.assign_first()
        self.verdicts["rows"][1].update(status="MISSING", reason="reveal",
                                         outcome="confirmed_failure", effect="zero")
        self.refresh_bindings()
        report = self.export(out_dir=None)["report"]
        self.assertEqual(report["verification"]["required_submissions"], 1)
        self.assertEqual(report["verification"]["counts"]["VERIFIED"], 1)
        self.assertEqual(report["verification"]["counts"]["MISSING"], 0)
        self.assertEqual(report["verification"]["status"], "verified")
        self.assertEqual(report["verification"]["verdicts_sha256"], artifact_digest(self.verdicts))
        self.assertNotIn("assignments", json.dumps(report))

    def test_assignment_selection_uses_incarnation_and_round_not_uid_or_hotkey_alone(self):
        self.assign_first()
        original = deepcopy(self.request["assignments"][0])
        # Moving a UID does not change the same registered hotkey/owner incarnation.
        for edits, expected in (({"uid": 99}, 1), ({"coldkey": "f" * 64}, 0),
                                ({"registration_block": original["registration_block"] + 1}, 0)):
            self.request["assignments"] = [{**original, **edits}]
            self.refresh_bindings()
            with self.subTest(edits=edits):
                report = self.export(out_dir=None)["report"]
                self.assertEqual(report["verification"]["required_submissions"], expected)
                self.assertEqual(report["verification"]["status"], "verified" if expected else "no_submissions")
        self.request["policy"]["required_rounds"].append({**self.contract, "reported_netuid": 8})
        self.request["assignments"] = [{**original, "targets": [8]}]
        self.refresh_bindings()
        self.assertEqual(self.export(out_dir=None)["report"]["verification"]["status"], "no_submissions")

    def test_empty_assignments_are_not_historical_full_roster(self):
        self.assign_first()
        self.request["assignments"] = []
        self.refresh_bindings()
        ver = self.export(out_dir=None)["report"]["verification"]
        self.assertEqual(ver["required_submissions"], 0)
        self.assertEqual(sum(ver["counts"].values()), 0)
        self.assertEqual(ver["status"], "no_submissions")

    def test_assignments_do_not_override_real_assigned_missing_or_unresolved_proof(self):
        self.assign_first()
        self.verdicts["rows"][0].update(status="MISSING", reason="reveal",
                                         outcome="confirmed_failure", effect="zero")
        self.refresh_bindings()
        ver = self.export(out_dir=None)["report"]["verification"]
        self.assertEqual(ver["counts"]["MISSING"], 1)
        self.assertEqual(ver["status"], "not_verified")
        # Keep the conservative gate even for an unresolved unassigned member.
        self.summary["resolved"] = False
        with self.assertRaisesRegex(exporter.PublicExportError, "round must be resolved"):
            self.export(out_dir=None)

    def test_assignment_request_cannot_be_unbound_or_malformed(self):
        self.assign_first()
        self.request["assignments"][0]["coldkey"] = "e" * 64
        with self.assertRaisesRegex(exporter.PublicExportError, "result binding"):
            self.export(out_dir=None)
        self.request["assignments"][0]["targets"] = [99]
        self.refresh_bindings()
        with self.assertRaisesRegex(exporter.PublicExportError, "invalid assignment request"):
            self.export(out_dir=None)

    def test_assignment_subset_cannot_discard_unassigned_roster_or_verdict_proof(self):
        self.assign_first()
        self.required["required_members"] = self.required["required_members"][:1]
        self.verdicts["rows"] = self.verdicts["rows"][:1]
        self.refresh_bindings()
        with self.assertRaisesRegex(exporter.PublicExportError, "complete snapshot roster coverage"):
            self.export(out_dir=None)

    def test_refuses_default_off_and_unpinned_receipt_before_verification(self):
        for kwargs in ({"activation_receipt": None}, {"activation_receipt_sha256": None}):
            with self.assertRaises(exporter.PublicExportError):
                self.export(**kwargs)
        self.assertEqual(self.calls, [])

    def test_activation_off_wrong_policy_future_or_unknown_field_refuse(self):
        original = deepcopy(self.receipt)
        for edit in ({"publication_approved": False}, {"report_enforcement": False},
                     {"report_enforcement": 1}, {"unknown": 1}, {"mining_netuid": 2},
                     {"policy_sha256": "a" * 64}, {"effective_day": original["effective_day"] + 1},
                     {"approved_at": "2026-11-15T00:01:00Z"}):
            self.receipt = {**original, **edit}; self.write_receipt()
            with self.subTest(edit=edit), self.assertRaises(exporter.PublicExportError):
                self.export()
        self.assertFalse(self.output.exists())

    def test_activation_digest_tampering_and_symlink_refuse(self):
        self.receipt_path.write_bytes(self.receipt_path.read_bytes() + b" ")
        with self.assertRaises(exporter.PublicExportError): self.export()
        self.write_receipt()
        target = self.root / "receipt-copy.json"; target.write_bytes(self.receipt_path.read_bytes())
        self.receipt_path.unlink(); self.receipt_path.symlink_to(target)
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_receipt_duplicate_keys_floats_and_negative_zero_refuse(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"effective_day":1.0}', b'{"effective_day":1e0}', b'{"effective_day":-0}'):
            self.receipt_path.write_bytes(raw); self.receipt_sha = hashlib.sha256(raw).hexdigest()
            with self.subTest(raw=raw), self.assertRaises(exporter.PublicExportError): self.export()

    def test_testnet_old_deposit_policy_and_synthetic_release_refuse(self):
        for field, value in (("genesis_hash", "f" * 64), ("mining_netuid", 2)):
            original = self.request[field]; self.request[field] = value
            with self.assertRaises(exporter.PublicExportError): self.export()
            self.request[field] = original
        self.contract["scoring_release"] = "synthetic-release"; self.refresh_bindings()
        with self.assertRaises(exporter.PublicExportError): self.export()
        self.contract["scoring_release"] = "points-v20"
        self.request["policy"]["eligibility_policy_version"] = "attributed-deposit-before-b-v1"
        self.refresh_bindings()
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_verifier_failure_and_forged_saved_manifest_cannot_publish(self):
        def failed(*args, **kwargs): raise ValueError("raw reconstruction refused")
        with self.assertRaisesRegex(ValueError, "raw reconstruction refused"):
            self.export(verifier=failed)
        self.result["request_sha256"] = "0" * 64
        with self.assertRaises(exporter.PublicExportError): self.export()
        self.assertFalse(self.output.exists())

    def test_changed_engine_verdict_roster_or_manifest_binding_refuses(self):
        for field in ("engine_sha256", "verdicts_sha256", "required_roster_sha256"):
            original = self.summary[field]; self.summary[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(exporter.PublicExportError): self.export()
            self.summary[field] = original
        self.result["manifest_sha256"] = "0" * 64
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_missing_duplicate_or_extra_final_verdict_refuses_even_with_rehashed_envelopes(self):
        rows = deepcopy(self.verdicts["rows"])
        for changed in (rows[:1], [rows[0], rows[0]], [*rows, rows[0]]):
            self.verdicts["rows"] = changed; self.refresh_bindings()
            with self.subTest(length=len(changed)), self.assertRaises(exporter.PublicExportError): self.export()

    def test_current_scored_subset_cannot_replace_full_snapshot_roster(self):
        self.required["required_members"] = self.required["required_members"][:1]
        self.verdicts["rows"] = self.verdicts["rows"][:1]
        self.refresh_bindings()
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_unresolved_cutoff_wrong_engine_context_and_reader_disagreement_refuse(self):
        self.summary["resolved"] = False
        with self.assertRaises(exporter.PublicExportError): self.export()
        self.summary["resolved"] = True
        self.engine["block_number"] += 1; self.refresh_bindings()
        with self.assertRaises(exporter.PublicExportError): self.export()
        self.engine["block_number"] -= 1
        self.verdicts["rows"][0]["sources"]["reader_b"]["verdict_sha256"] = "0" * 64
        self.refresh_bindings()
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_accurate_missing_late_incorrect_counts_and_statuses(self):
        for status, reason in (("MISSING", "reveal"), ("LATE", "reveal"), ("MISMATCH", "outputs_differ"),
                               ("EARLY", "commit"), ("MALFORMED", "payload_encoding")):
            self.verdicts["rows"][0].update(status=status, reason=reason, outcome="confirmed_failure", effect="zero")
            self.refresh_bindings()
            report = self.export(out_dir=None)["report"]
            self.assertEqual(report["verification"]["counts"][status], 1)
            self.assertEqual(report["verification"]["status"], "partial")
        for row in self.verdicts["rows"]:
            row.update(status="MISSING", reason="reveal", outcome="confirmed_failure", effect="zero")
        self.refresh_bindings()
        self.assertEqual(self.export(out_dir=None)["report"]["verification"]["status"], "not_verified")

    def test_zero_required_and_allowed_chain_unavailability(self):
        self.required["required_members"] = []
        self.required["snapshot"]["members"] = []
        self.verdicts["rows"] = []
        self.refresh_bindings()
        self.assertEqual(self.export(out_dir=None)["report"]["verification"]["status"], "no_submissions")
        self.engine.update(status="UNSUPPORTED", reason="stable_mechanism")
        self.refresh_bindings()
        result = self.export(out_dir=None)["report"]
        self.assertEqual(result["verification"]["status"], "calculation_unavailable")
        self.assertIsNone(result["calculation"]["output1"])

    def test_operational_failures_partial_calculation_matches_and_invalid_units_refuse(self):
        original = deepcopy(self.engine)
        for edit in ({"status": "UNAVAILABLE", "reason": "rpc_transport"},
                     {"output2": {"status": "UNAVAILABLE", "reason": "search_budget_exceeded"}},
                     {"output1": {"status": "OK", "reason": None, "drop_ppm": 1000001}},
                     {"output2": {"status": "OK", "reason": None, "alpha_required_rao": 1}}):
            self.engine.clear(); self.engine.update(deepcopy(original)); self.engine.update(edit)
            self.refresh_bindings()
            with self.subTest(edit=edit), self.assertRaises(exporter.PublicExportError): self.export()

    def test_idempotent_bytes_and_explicit_correction_preserve_history(self):
        first = self.export()
        self.assertFalse(self.export()["created"])
        self.assertEqual(len(list(self.output.glob("*.json"))), 1)
        with self.assertRaises(exporter.PublicExportError):
            self.export(published_at="2026-11-15T02:00:00Z")
        correction = self.export(published_at="2026-11-15T02:00:00Z", supersedes=first["report"]["report_id"])
        self.assertTrue(correction["created"])
        self.assertEqual(Path(first["path"]).read_bytes(), first["json_bytes"])
        self.assertFalse(self.export(published_at="2026-11-15T02:00:00Z", supersedes=first["report"]["report_id"])["created"])
        with self.assertRaises(exporter.PublicExportError):
            self.export(published_at="2026-11-15T03:00:00Z", supersedes=first["report"]["report_id"])

    def test_unknown_correction_target_and_tampered_retained_file_refuse(self):
        with self.assertRaises(exporter.PublicExportError): self.export(supersedes="0" * 64)
        first = self.export()
        path = Path(first["path"])
        changed = deepcopy(first["report"]); changed["chain"]["spec_version"] = 491
        path.write_text(json.dumps(changed))
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_competing_corrections_cannot_fork_public_history(self):
        first = self.export()
        ctx = multiprocessing.get_context("spawn")
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = []
        try:
            for hour in (2, 3):
                candidate = self.export(out_dir=None, published_at=f"2026-11-15T0{hour}:00:00Z",
                                        supersedes=first["report"]["report_id"])
                process = ctx.Process(target=competing_correction, args=(self.output, self.package,
                    candidate["report"], candidate["json_bytes"], self.now_ms, ready, start, results))
                process.start()
                processes.append(process)
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            self.assertEqual(sorted(results.get(timeout=10) for _ in processes), ["created", "refused"])
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(list(self.output.glob("*.json"))), 2)
            self.assertEqual(Path(first["path"]).read_bytes(), first["json_bytes"])
        finally:
            start.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10)
            ready.close()
            results.close()

    def test_public_output_cannot_be_private_package_or_symlink(self):
        with self.assertRaises(exporter.PublicExportError): self.export(out_dir=self.package / "public")
        self.output.symlink_to(self.package, target_is_directory=True)
        with self.assertRaises(exporter.PublicExportError): self.export()

    def test_publication_before_evidence_or_in_future_refuses(self):
        for published in ("2026-11-15T00:30:00Z", "2026-11-17T01:00:00Z", "2026-02-30T01:00:00Z"):
            with self.subTest(published=published), self.assertRaises(exporter.PublicExportError): self.export(published_at=published)

    def test_real_exporter_bytes_are_accepted_by_website_consumer(self):
        # Website and validator changes intentionally use separate worktrees.
        # A qualification owner may select that reviewed checkout explicitly.
        repo = Path(os.environ.get('NOAH_REPORT_WEBSITE_ROOT', Path(__file__).resolve().parents[5]))
        tool = repo / "apps/website/tools/validate-backing-export.mjs"
        if not tool.is_file() or shutil.which("node") is None:
            self.skipTest("private website consumer integration tool is outside this source package")
        result = self.export()
        self.assertEqual(result["report"]["report_id"],
                         "6631c73940675a2703938e8c9620c2159d7bdea644b769039ccbd6ed8bf607ca")
        self.assertEqual(result["filename"], "2026-11-15-sn7-6631c7394067.json")
        consumer = subprocess.run(["node", str(tool), result["path"]], capture_output=True, text=True)
        self.assertEqual(consumer.returncode, 0, consumer.stderr)
        checked = json.loads(consumer.stdout)
        self.assertEqual(checked["validation"], "public-shape-and-history-only")
        self.assertEqual(checked["reports"], [result["report"]["report_id"]])
        self.assertEqual(checked["selected"], [{"netuid": 7, "status": "verified"}])


if __name__ == "__main__":
    unittest.main()
