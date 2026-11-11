"""Explicit opt-in points policy on the existing history/publication pipeline.

The existing immutable receipt history remains the only source of balances.
Each closed day is replayed; no second mutable balance database is introduced.
"""

import json
from pathlib import Path

from ..chain.entry_history import selected as entry_history
from ..chain.foreign_value import ForeignDepositUnpriced
from ..chain.sources import Refusal, SourceDisagreement, SourceLied, Unverified
from ..core.calculation_record import digest, durable_write, encoded
from ..core.settings import SettingsError
from . import amendment, ema_policy, onboarding
from .collector import Collector
from .engine import POLICY, evaluate
from .migration import validate
from .mode import MODE, source_mode
from .valuation import credit

MODES = (MODE, amendment.MODE, ema_policy.MODE, onboarding.MODE)

PROFILE = "mainnet-production-points-402040-v1"


def validate_configuration(settings):
    """The selected mode, profile and migration are one atomic configuration."""
    from ..scoring.parameters import load_parameters

    mode = getattr(settings, "scoring_mode", "legacy")
    path = getattr(settings, "points_policy", None)
    if mode not in ("legacy", *MODES):
        raise SettingsError("unsupported scoring mode")
    if (mode in MODES) != bool(path):
        raise SettingsError("points scoring mode and NOAH_POINTS_POLICY must be selected together")
    try:
        profile = load_parameters().profile_version
    except ValueError as exc:
        raise SettingsError("scoring mode requires a readable parameter profile") from exc
    expected = onboarding.PROFILE if mode == onboarding.MODE else PROFILE
    if (mode in MODES and profile != expected) or (
        mode == "legacy" and profile in (PROFILE, onboarding.PROFILE)
    ):
        raise SettingsError("scoring mode and parameter profile differ")
    return mode


def configure(adapter, settings, *, persist=True, amendment_transition=False):
    if amendment_transition and persist:
        raise ValueError("amendment preview cannot persist")
    adapter.scoring_mode = validate_configuration(settings)
    path = getattr(settings, "points_policy", None)
    if not path:
        if getattr(adapter, "_submitted_record_dir", None):
            _check_cutover_state(adapter, "legacy")
        return
    try:
        from ..core.version import version_key_for
        from ..scoring.parameters import load_parameters

        if load_parameters().profile_version != (
            onboarding.PROFILE if onboarding.enabled(adapter) else PROFILE
        ):
            raise ValueError("points requires its explicit parameter profile")
        if (
            not entry_history(adapter)
            or not adapter.epoch_collection
            or not adapter.epoch_republication
        ):
            raise ValueError(
                "points requires frozen entry history, epoch collection and refresh"
            )
        if not adapter._submitted_record_dir:
            raise ValueError("durable calculation evidence required")
        document = json.loads(Path(path).read_text())
        expected_sha = (
            onboarding.manifest(ema_policy.manifest(document, adapter.netuid))["sha256"]
            if onboarding.enabled(adapter)
            else digest(encoded(document))
        )
        _check_cutover_state(adapter, adapter.scoring_mode, expected_sha)
        policy = RuntimePolicy(document, adapter, amendment_transition=amendment_transition)
        if policy.manifest["scoring_mode"] != adapter.scoring_mode:
            raise ValueError("policy document and selected mode differ")
        key = version_key_for(policy.manifest["policy"] + "/" + policy.manifest["sha256"])
        floor = adapter.runner_chain.starter_epoch_info(adapter.runner_chain.head_block())[
            "WeightsVersionKey"
        ]
        if type(floor) is not int or key < floor:
            raise ValueError("points publication key below chain floor")
        adapter.publisher.version_key = key
        adapter.points_policy = policy
        if persist:
            policy.persist()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SettingsError(
            "points policy, migration or parameter profile invalid: " + str(exc)
        ) from None


def _check_cutover_state(adapter, mode, policy_sha=None):
    """Do not select a new mode over an unresolved old send or starter overlay."""
    root = Path(adapter._submitted_record_dir)
    if mode == "legacy" and (root / "points-policy.json").exists():
        raise SettingsError(
            "points accounting is installed; legacy requires a reviewed reverse migration"
        )
    intent = getattr(adapter, "_publish_intent", None)
    pending = intent.load() if intent is not None else None
    if pending is not None:
        reference = pending.get("calculation_evidence")
        # Historical legacy sends may predate retained calculation evidence.
        pending_mode, context = "legacy", {}
        if reference is not None:
            pending_mode, context, _ = source_mode(adapter._submitted_record_dir, reference)
        if pending_mode != mode or (
            mode in MODES and context.get("points_policy", {}).get("sha256") != policy_sha
        ):
            raise SettingsError("reconcile the pending publication before changing scoring mode")
    path = Path(adapter._submitted_record_dir) / "starter-active.json"
    if path.exists():
        # Read-only compatibility for an unresolved pre-cutover publication.
        from ..core.calculation_record import historical_overlay
        active = json.loads(path.read_text())
        proof = historical_overlay(adapter._submitted_record_dir, active["reference"])
        if proof is not None and proof["mode"] == "starter-overlay":
            active_mode, context, _ = source_mode(
                adapter._submitted_record_dir, active["reference"]
            )
            if active_mode != mode or (
                mode in MODES and context.get("points_policy", {}).get("sha256") != policy_sha
            ):
                raise SettingsError("remove the previous mode's starter overlay before switching")


class RuntimePolicy:
    def __init__(self, document, adapter, *, amendment_transition=False):
        self.no_pool = onboarding.enabled(adapter)
        if self.no_pool and document.get("schema") != ema_policy.SCHEMA:
            raise ValueError("ordinary onboarding requires the authenticated EMA policy")
        self.day_anchor_ts = adapter.day_anchor_ts
        supplied_document = document
        self.ema = document.get("schema") == ema_policy.SCHEMA
        self.ema_document = document if self.ema else None
        self.amended = self.ema or document.get("schema") == amendment.SCHEMA
        document = ema_policy.baseline(document) if self.ema else amendment.baseline(document)
        if (
            set(document)
            != {
                "schema",
                "migration",
                "fixture",
                "historical_valuations",
                "pricing",
                "ark_address",
                "validator_hotkey",
                "lineages",
            }
            or document["schema"] != "noah-points-policy/2"
        ):
            raise ValueError("unsupported points policy document")
        m = document["migration"]
        chain = adapter.runner_chain
        if type(m.get("cutoff_block")) is not int or m["cutoff_block"] + 1 > chain.head_block():
            raise ValueError("migration cutoff and successor must be finalized by both readers")
        validate(
            m,
            expected_genesis=chain.genesis_hash(),
            expected_cutoff_hash=chain.block_hash(m["cutoff_block"]),
        )
        if (
            document["ark_address"] != adapter.ark_coldkey
            or document["validator_hotkey"] != adapter.designated_validator_hotkey
        ):
            raise ValueError("points destination identity mismatch")
        fixture = document["fixture"]
        # Migration accepts a specific source serialization, pinned in the manifest.
        import hashlib

        if (
            hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
            != m["source_sha256"]
        ):
            raise ValueError("migration source digest differs")
        if (
            int(fixture["evalDay"]) != m["cutoff_day"]
            or int(fixture["dayEpochTs"]) != adapter.day_anchor_ts
        ):
            raise ValueError("migration cutoff/UTC anchor differs")
        cutoff_time = chain.block_timestamp(m["cutoff_block"])
        successor_time = chain.block_timestamp(m["cutoff_block"] + 1)
        start = adapter.day_anchor_ts + m["cutoff_day"] * 86400
        end = start + 86400
        if (
            type(cutoff_time) is not int
            or type(successor_time) is not int
            or not start <= cutoff_time < end <= successor_time
        ):
            raise ValueError("migration cutoff must be the last finalized block of its UTC day")
        candidate = {r["uid"]: r for r in evaluate(fixture)["miners"]}
        if set(candidate) != {r["uid"] for r in m["miners"]}:
            raise ValueError("migration roster incomplete")
        from ..scoring.engine import score_miner
        from ..scoring.fixture_io import parse_fixture

        fx = parse_fixture(fixture)
        legacy = {r.uid: score_miner(r, fx.dials, fx.eval_day) for r in fx.miners}
        seats = chain.seat_hotkeys(m["cutoff_block"])
        owners = chain.hotkey_owners(list(seats.values()), m["cutoff_block"])
        for row in m["miners"]:
            if (
                seats.get(row["uid"]) != row["hotkey"]
                or owners.get(row["hotkey"]) != row["coldkey"]
            ):
                raise ValueError("migration identity differs from finalized roster")
            before = legacy[row["uid"]]
            if (
                before.coverage_reserve_rao != row["legacy_coverage_rao"]
                or before.debt_rao != row["legacy_debt_rao"]
            ):
                raise ValueError("legacy prepaid/debt reconciliation failed")
        from .lineages import validate_document

        self.baselines = validate_document(document, netuid=adapter.netuid)
        self.frozen = {key: row["input"] for key, row in self.baselines.items()}
        historical_blocks = {}
        for lineage in document["lineages"]["inventory"]["pairs"]:
            historical_blocks.setdefault(lineage["block"], []).append(lineage)
        for block, lineages in historical_blocks.items():
            block_hash = chain.block_hash(block)
            owned = [row["hotkey"] for row in lineages if "receipt" not in row]
            owners = chain.hotkey_owners(owned, block) if owned else {}
            for lineage in lineages:
                if block_hash != lineage["block_hash"]:
                    raise ValueError("historical lineage attribution differs from both readers")
                if "receipt" in lineage:
                    from .lineage_receipt import verify as verify_receipt

                    verify_receipt(
                        chain,
                        lineage,
                        document,
                        netuid=adapter.netuid,
                        day_anchor_ts=adapter.day_anchor_ts,
                    )
                elif owners.get(lineage["hotkey"]) != lineage["coldkey"]:
                    raise ValueError("historical lineage attribution differs from both readers")
        entry = entry_history(adapter)
        if entry is not None:
            snapshot = getattr(entry, "snapshot", {})
            if snapshot.get("sha256") != document["lineages"]["entry_snapshot_sha256"]:
                raise ValueError(
                    "lineage entry classification differs from installed frozen snapshot"
                )
            entries = {
                (row["coldkey"], row["hotkey"])
                for row in snapshot.get("payload", {}).get("records", [])
            }
            captured_entries = {
                (row["coldkey"], row["hotkey"]) for row in document["lineages"]["entry_pairs"]
            }
            if entries != captured_entries:
                raise ValueError("lineage entry identities differ from installed frozen snapshot")
        for row in m["miners"]:
            # Preserve the authoritative legacy liability, never replay a new
            # historical credit as debt forgiveness or a fresh contribution.
            candidate[row["uid"]]["state"]["balance"] = str(
                row["legacy_coverage_rao"] - row["legacy_debt_rao"]
            )
            if (
                row["uid"] not in candidate
                or candidate[row["uid"]]["coldkey"] != row["coldkey"]
                or candidate[row["uid"]]["state"] != {k: str(v) for k, v in row["state"].items()}
            ):
                raise ValueError("migration balances differ from history replay")
        p = document["pricing"]
        if p != {
            "method": "previous-completed-utc-day-average",
            "assets": "all-subnet-tokens-and-root",
            "credit": "actual-received",
        }:
            raise ValueError("unsupported daily reference policy")
        self.migration = m
        self.old = {}
        for v in document["historical_valuations"] + document["lineages"]["foreign_valuations"]:
            key = (v["block"], v["block_hash"], v["token_netuid"], int(v["amount_rao"]))
            amount = int(v["noah_equivalent_rao"])
            if key in self.old and self.old[key] != amount:
                raise ValueError("historical valuation conflict")
            self.old[key] = amount
        if sum(int(v["noah_equivalent_rao"]) for v in document["historical_valuations"]) != sum(
            int(day["foreignDepositNoahEquivRao"])
            for miner in fixture["miners"]
            for day in miner["days"].values()
        ):
            raise ValueError(
                "historical foreign valuations do not reconcile with credited balances"
            )
        self.manifest = dict(
            policy=amendment.POLICY if self.amended else POLICY,
            scoring_mode=amendment.MODE if self.amended else MODE,
            netuid=adapter.netuid,
            sha256=digest(encoded(supplied_document)),
            cutoff_day=m["cutoff_day"],
            cutoff_block=m["cutoff_block"],
            pricing=p,
        )
        if self.ema:
            self.manifest = ema_policy.manifest(supplied_document, adapter.netuid)
            anchor = supplied_document["activation_anchor"]
            if (
                anchor["block"] > chain.head_block()
                or chain.block_hash(anchor["block"]) != anchor["hash"]
            ):
                raise ValueError("EMA activation anchor is not agreed finalized history")
        root = Path(adapter._submitted_record_dir)
        # The same policy on restart is idempotent. A changed policy never silently
        # rewrites earlier balances or permits unreviewed repricing.
        self.path = root / "points-policy.json"
        self.document_bytes = encoded(supplied_document)
        if amendment_transition:
            (ema_policy if self.ema else amendment).check_transition(adapter, supplied_document)
        else:
            if self.amended and not self.path.is_file():
                raise ValueError("amended policy must be explicitly installed before startup")
            old_envelope = (
                ema_policy.validate_document(supplied_document) if self.ema else supplied_document
            )
            if self.ema and (
                root / "points-policy-ema-predecessor.json"
            ).read_bytes() != supplied_document["predecessor_json"].encode("utf-8"):
                raise ValueError("retained EMA predecessor bytes differ")
            if self.amended and (
                root / "points-policy-predecessor.json"
            ).read_bytes() != old_envelope["predecessor_json"].encode("utf-8"):
                raise ValueError("retained predecessor bytes differ")
            if self.path.exists() and self.path.read_bytes() != self.document_bytes:
                raise ValueError("existing points policy differs; explicit migration required")
        if self.no_pool:
            self.manifest = onboarding.manifest(self.manifest)
        self.transition_preview = amendment_transition
        self.collector = Collector(chain, root / "points-prices", adapter.netuid)
        if self.amended:
            from .eligibility import ReferenceEligibility

            self.collector.eligibility = ReferenceEligibility(chain, adapter.netuid)
        if self.ema:
            from .ema_reference import EmaReference

            self.ema_collector = EmaReference(
                chain, root, adapter.netuid, supplied_document["ema_source"]
            )

    @property
    def input_schema(self):
        return (
            ema_policy.INPUT_SCHEMA
            if self.ema
            else amendment.INPUT_SCHEMA
            if self.amended
            else "noah-points-fixture/1"
        )

    def persist(self):
        """Install only after successful runtime configuration, never in preflight."""
        if self.transition_preview:
            raise ValueError("transition preview cannot install a policy")
        durable_write(self.path, self.document_bytes)

    def check_snapshot(self, adapter, fixture, hotkeys):
        if int(fixture["evalDay"]) <= self.migration["cutoff_day"]:
            raise Refusal("points scoring begins after the pinned legacy cutoff day")
        # Bind by identity, not UID. A newly occupied UID cannot inherit maturity.
        old = self.baselines
        cutoff = self.migration["cutoff_day"]
        for row in fixture["miners"]:
            if row["uid"] is None:
                continue
            pair = (row["coldkey"], hotkeys[row["uid"]])
            saved = old.get(pair)
            if saved is None:
                # firstActivityDay can be wallet-wide. A fresh hotkey may have
                # empty pre-cutoff rows, but actual old financial history needs
                # an authenticated migration baseline even if absent at cutoff.
                # Locked arrivals alone are wallet-wide fixture metadata, not
                # this hotkey's earned/spendable obligation or deposit history.
                monetary = (
                    "paidRao",
                    "payUnlockedRao",
                    "noahDepositRao",
                    "foreignDepositNoahEquivRao",
                )
                activity = any(
                    int(value[field]) != 0
                    for day, value in row["days"].items()
                    if int(day) <= cutoff
                    for field in monetary
                )
                old_entry = (
                    int(row["entryDepositRao"]) != 0
                    and int(row.get("registrationDay", row["firstActivityDay"])) <= cutoff
                )
                if activity or old_entry:
                    raise Refusal("pre-migration financial lineage lacks an authenticated baseline")
                continue
            if int(row["firstActivityDay"]) > cutoff:
                if self._empty_lineage(saved):
                    continue
                raise Refusal("existing identity lost pre-migration history")
            historical = {**row, "days": {d: v for d, v in row["days"].items() if int(d) <= cutoff}}
            frozen = self.frozen[pair]
            if historical["days"] != frozen["days"] or (
                historical["entryDepositRao"] != frozen["entryDepositRao"]
                and not (
                    historical["entryDepositRao"] == "0"
                    and int(historical["registrationDay"]) > cutoff
                )
            ):
                raise Refusal("pre-migration receipts/earnings/entry changed")
            prefix = {
                **fixture,
                "schema": "noah-fixture/6",
                "evalDay": str(cutoff),
                "miners": [historical],
            }
            prefix.pop("pointsReceipts", None)
            prefix.pop("pointsHotkeys", None)
            prefix.pop("pointsOwners", None)
            prefix.pop("pointsPricing", None)
            prefix.pop("pointsPricingBoundary", None)
            state = evaluate(prefix)["miners"][0]["state"]
            state["balance"] = str(saved["legacy_coverage_rao"] - saved["legacy_debt_rao"])
            if state != {k: str(v) for k, v in saved["state"].items()}:
                raise Refusal("pre-migration identity history changed")
        return True

    @staticmethod
    def _empty_lineage(saved):
        # A never-funded/never-earned frozen entry identity has no financial
        # prefix to carry. Real readers may first observe it after the cutoff.
        # The last cutoff day's earnings still matter tomorrow, even when the
        # cutoff state itself is empty, so check the full own monetary history.
        from dataclasses import asdict

        from .ledger import State

        raw = saved["input"]
        return (
            saved["state"] == asdict(State())
            and raw["entryDepositRao"] == "0"
            and saved["legacy_coverage_rao"] == saved["legacy_debt_rao"] == 0
            and not any(
                int(day[field])
                for day in raw["days"].values()
                for field in (
                    "paidRao",
                    "payUnlockedRao",
                    "noahDepositRao",
                    "foreignDepositNoahEquivRao",
                )
            )
        )

    def fixture_migration(self, fixture, hotkeys):
        old = self.baselines
        balances = []
        for row in fixture["miners"]:
            if row["uid"] is None:
                continue
            saved = old.get((row["coldkey"], hotkeys[row["uid"]]))
            if saved is not None:
                if int(row["firstActivityDay"]) > self.migration[
                    "cutoff_day"
                ] and self._empty_lineage(saved):
                    continue
                balances.append(
                    dict(
                        uid=row["uid"],
                        coldkey=row["coldkey"],
                        balanceRao=str(saved["state"]["balance"]),
                    )
                )
        return dict(
            policySha256=self.manifest["sha256"],
            cutoffDay=self.migration["cutoff_day"],
            balances=sorted(balances, key=lambda row: row["uid"]),
        )

    def precollect_foreign(self, receipts):
        """Authenticate the exact accepted prospective inputs, then collect by day.

        This is price-series preparation only. The scalar rule still values each
        receipt separately and emits the original evidence in fixture order.
        """
        receipts = list(receipts)
        if getattr(self, "ema", False):
            self.ema_collector.reset()
            upcoming = [r for r in receipts if r[2] >= self.ema_document["effective_block"]]
            for _, _, block, block_hash in upcoming:
                if self.collector.chain.block_hash(block) != block_hash:
                    raise SourceLied("EMA receipt block hash differs")
            try:
                self.ema_collector.precollect((r[2], r[1]) for r in upcoming)
            except (SourceDisagreement, SourceLied, Unverified):
                raise
            except Refusal as exc:
                raise ForeignDepositUnpriced("EMA reference unavailable; preserve receipt") from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise SourceLied(
                    "EMA reference validation contradicted collected evidence"
                ) from exc
            receipts = [r for r in receipts if r[2] < self.ema_document["effective_block"]]
        c = self.collector
        if hasattr(c, "eligibility"):
            c.eligibility.reset()
        authenticated = []
        for amount, netuid, block, block_hash in receipts:
            if block <= self.migration["cutoff_block"]:
                continue  # Frozen history is never repriced.
            if c.chain.block_hash(block) != block_hash:
                raise Refusal("receipt block hash differs from both readers")
            try:
                c.chain.block_timestamp(block)
                if block > c.chain.head_block():
                    raise Refusal("receipt is not finalized by both readers")
            except (SourceDisagreement, SourceLied, Unverified):
                raise
            except (Refusal, ValueError, OSError, KeyError) as exc:
                raise ForeignDepositUnpriced(
                    "points pricing evidence unavailable; preserve receipt and retry"
                ) from exc
            authenticated.append((block, netuid))
        try:
            groups = {}
            windows = {}
            for block, netuid in authenticated:
                if block not in windows:
                    windows[block] = tuple(c._window(block))
                window = windows[block]
                # Equal timestamps at UTC midnight can give different scalar
                # last-block keys. Group by the complete authenticated window,
                # not merely day, so every existing cache identity is preserved.
                group = groups.setdefault(
                    window, {"block": block, "assets": set(), "receipt_blocks": {}}
                )
                group["assets"].add(netuid)
                group["receipt_blocks"].setdefault(netuid, block)
            for window in sorted(groups):
                group = groups[window]
                kwargs = (
                    {"receipt_blocks": group["receipt_blocks"]} if hasattr(c, "eligibility") else {}
                )
                c.precollect(group["block"], group["assets"], expected_window=window, **kwargs)
        except (SourceDisagreement, SourceLied, Unverified):
            raise
        except (Refusal, ValueError, OSError, KeyError) as exc:
            raise ForeignDepositUnpriced(
                "points pricing evidence unavailable; preserve receipt and retry"
            ) from exc

    def foreign_rule(self, adapter, legacy):
        def rule(amount, netuid, block, block_hash, *, receipt=None):
            if block <= self.migration["cutoff_block"]:
                key = (block, block_hash, netuid, amount)
                if key not in self.old:
                    raise ForeignDepositUnpriced(
                        "pre-migration foreign receipt missing frozen valuation"
                    )
                return self.old[key]
            c = self.collector
            if c.chain.block_hash(block) != block_hash:
                raise Refusal("receipt block hash differs from both readers")
            try:
                timestamp = c.chain.block_timestamp(block)
                outcome = None
                if getattr(self, "ema", False) and block >= self.ema_document["effective_block"]:
                    from .ema_reference import credit as ema_credit

                    if receipt is None:
                        raise ValueError("EMA receipt requires exact event identity")
                    outcome = self.ema_collector.result(block, netuid)
                    decision = dict(
                        receipt_id=f"{c.genesis}/{block_hash}/{receipt['event_ordinal']}",
                        status="accepted" if outcome["status"] == "supported" else "unsupported",
                        reason="ema-spot-reference"
                        if outcome["status"] == "supported"
                        else outcome["reason"],
                        credit_rao=ema_credit(outcome["document"], amount),
                    )
                elif getattr(self, "amended", False):
                    if receipt is None:
                        raise ValueError("amended receipt requires exact event identity")
                    outcome = c.eligibility.result(c, block, netuid)
                    if outcome["status"] == "unsupported":
                        decision = dict(
                            receipt_id=f"{c.genesis}/{block_hash}/{receipt['event_ordinal']}",
                            status="unsupported",
                            reason=outcome["reason"],
                            credit_rao=0,
                        )
                    else:
                        decision = credit(
                            receipt_id=f"{c.genesis}/{block_hash}/{receipt['event_ordinal']}",
                            amount_rao=amount,
                            netuid=netuid,
                            timestamp=timestamp,
                            intervals=c.prior_day(block, netuid),
                            native_netuid=adapter.netuid,
                        )
                else:
                    decision = credit(
                        receipt_id=f"{block_hash}/{netuid}/{amount}",
                        amount_rao=amount,
                        netuid=netuid,
                        timestamp=timestamp,
                        intervals=c.prior_day(block, netuid),
                        native_netuid=adapter.netuid,
                    )
            except (SourceDisagreement, SourceLied, Unverified):
                raise
            except (ValueError, KeyError, TypeError) as exc:
                if getattr(self, "ema", False) and block >= self.ema_document["effective_block"]:
                    raise SourceLied("EMA reference contradicts retained receipt evidence") from exc
                raise ForeignDepositUnpriced(
                    "points pricing evidence unavailable; preserve receipt and retry"
                ) from exc
            except (Refusal, OSError) as exc:
                raise ForeignDepositUnpriced(
                    "points pricing evidence unavailable; preserve receipt and retry"
                ) from exc
            adapter._foreign_valuation_evidence.append(
                dict(
                    block=block,
                    block_hash=block_hash,
                    token_netuid=netuid,
                    amount_rao=str(amount),
                    **decision,
                    policy_sha256=self.manifest["sha256"],
                    **(
                        {"reference_proof": outcome["proof"], "receipt": receipt}
                        if outcome is not None
                        else {}
                    ),
                )
            )
            if decision["status"] == "pending":
                raise ForeignDepositUnpriced("points pricing pending; no guessed daily score")
            return decision["credit_rao"]

        rule.for_receipt = lambda receipt: rule(
            int(receipt["amount_rao"]),
            receipt["token_netuid"],
            receipt["block"],
            receipt["block_hash"],
            receipt=receipt,
        )
        return rule
