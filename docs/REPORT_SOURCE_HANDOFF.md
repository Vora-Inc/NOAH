# Resume one frozen report after a producer upgrade

This is an operator recovery procedure for an already frozen, **unsent** report
request. It does not change scores, report deadlines, assignments, eligibility,
bonus rules or burn. Ordinary upgrades with no frozen request need no handoff.
Both settings below are absent by default; changing source without a handoff
continues to refuse. No new signing key is introduced.

A report-only code fix changes the full producer fingerprint. The validator will
not silently reinterpret the old request as a new producer's request. A handoff
binds one original request/R0 to one exact successor artifact and preserves the
original request bytes, evaluation snapshot, policy, assignments and financial
producer. The successor has a separate R0 audit record. R1 optionally links the
content-addressed handoff document; old proofs remain readable without that field.

## Prepare, review and install

1. Qualify the exact successor artifact through the normal release workflow.
   Stop all writers using the maintained held-release/checkpoint procedure. Keep
   the existing HOLD, publication/commit intents, history and request intact.
   A pending/uncertain send or completed-day disposition must be reconciled;
   this procedure cannot reopen it.
2. Authenticate the original frozen request and R0. Generate a **separate**
   successor R0 using the maintained ingestion and twin-calculation path against
   the unchanged financial cutoff. Do not manufacture its context or overwrite
   the old reference. Both R0s must have identical full financial context,
   fixture, agreed score, raw vectors and policy; only the producer and a later
   attempt height may differ.
3. Retain both exact archives and their managed installed release trees. Prepare
   two private JSON descriptors, each containing exactly `root`, `archive`,
   `archive_sha256`, and `manifest_sha256`. Use completed release receipts, not
   guessed IDs. `root` is the protected managed release directory containing
   `payload/` and `venv/`; its basename is the canonical release ID. All payload
   bytes/modes, the managed Python/library and source fingerprints are checked.
   The package root comes from the unique authenticated manifest entry ending
   in `noah/core/calculation_record.py`; missing, ambiguous or malformed roots
   refuse. No private checkout layout is assumed.
4. Using the successor's isolated validator environment, prepare a draft:

   ```sh
   noah-validator report-handoff \
     --scores /path/to/held/scores \
     --request /path/to/held/scores/report-state/day-D.request.json \
     --successor-reference /private/successor-reference.json \
     --old-release /private/old-release.json \
     --new-release /private/new-release.json \
     --approval 'reviewed operator change reference' \
     --checkpoint-sha256 ACTUAL_HELD_CHECKPOINT_SHA256 \
     --output /private/reviewed-handoff.json
   ```

   Equivalently invoke `python -I -B -m noah.core.report_handoff_prepare` from
   that installed environment. The command runs old Python, old TypeScript,
   successor Python and successor TypeScript against the original fixture; all
   four results must reproduce its exact agreed-score bytes. It also compares
   the separately generated successor R0's full financial evidence. It writes
   only a new draft after checks succeed and never installs config, clears HOLD,
   changes services or sends a transaction. A missing declared commit in old
   context stays `null`; the authenticated release manifest supplies a separate
   full commit binding.
5. Review the actual output and immutable graph. Under the maintained held
   release locks/journal, install the exact draft as a root-owned regular file
   (one link, no group/other write, protected root-owned ancestors), readable by
   the validator. Add **both implemented settings** to the protected validator
   configuration and saved backup allowlist:

   ```text
   NOAH_REPORT_SOURCE_HANDOFF=/etc/noah-validator/report-source-handoff.json
   NOAH_REPORT_SOURCE_HANDOFF_SHA256=<SHA256 of exact installed bytes>
   ```

   The checkpoint digest records the operator-reviewed maintenance evidence.
   It is not a runtime attestation that the host remains held after restart;
   the operator must verify the actual checkpoint before installing the pins.
   Runtime admission instead authenticates those protected pins, exact frozen
   financial graph and current producer, followed by the normal publication guards.

   This is the authorization boundary. A draft or a service-writable evidence
   file alone is not authorization. Reports must remain enabled when this pair
   is configured. Do not edit the request, R0 or evaluation to make admission
   pass. Verify the document, config and checkpoint again before the normal
   held-to-running release reconciliation. The command above deliberately
   cannot install these settings or act as the human approval step.
6. After restart, verify the retained R1's `source_handoff` reference, original
   request/R0, report replay and actual publication through the usual paired
   reconciliation. Every unconfirmed retry checks the running full source identity and
   current authorization pin. Normal policy, migration, maturity/debt,
   current miner identity and burn checks still apply.

The authorization is request-specific and supports idempotent retries; it is
not consumed or deleted on the first attempt. It cannot authorize another day,
request, source pair or changed economics. Subsequent days use the successor
normally. When removing the selected handoff or disabling reports, remove both
configuration settings together while preserving the immutable historical
handoff and original request/R0 evidence.

## Refresh a confirmed day after a later upgrade

A later qualified executable can refresh the exact already-confirmed daily
allocation without a new handoff. The original protected authorization, frozen
request, R0/R1 records and daily receipt remain unchanged. The validator must
independently reconfirm the canonical daily transaction against the finalized
chain, including its exact call, scoped success and complete stored vector.
A saved `confirmed` flag is insufficient.

This path applies only to a current epoch refresh with the same day, attempt
height, full calculation reference and submitted vector. It checks the current
policy, configuration, identities, rate limit, outage deadline and burn proof.
The check repeats before transport against the exact newly opened intent;
that attempt-local permission is discarded on success or failure. An uncertain
send must reconcile and cannot reuse it after restart.

A first send, a new burn-zero replacement or an unconfirmed retry still requires
the handoff's exact successor source. An already confirmed burn-zero replacement
can subsequently refresh its own unchanged allocation. This does not recalculate
the old report, change its verdict or bonus, reopen a completed day, or introduce
a manual-send bypass. The new artifact must pass the normal release checks.

## Backup and restore

When configured, the config bundle includes the actual
`report-source-handoff.json` member and checks its digest/network against the
allowlisted configuration. The score snapshot retains only the strict
`report-state/source-handoffs/<sha256>.json` namespace and verifies its original
and successor R0/request graph. Unknown paths, missing evidence, changed
requests or substituted documents refuse.

Restore retains the document in the fresh config directory and validates it
against the restored score graph. Before starting the restored validator,
install it at the configured protected path with root custody and appropriate
read permissions, restore both config pins, and follow the normal HOLD and
chain reconciliation procedure. Historical proof verification uses retained
bytes and does not depend on today's selected authorization.
