# Daily scoring and epoch publication

> Mainnet uses V2.1 (`points-204040-v6`, policy/6) from November 24, 2026 at 00:00 UTC (scoring day 1345). V2 (`points-204040-v5`, policy/5) remains the policy for its earlier results.

Under ordinary V2 and V2.1 scoring, daily calculations produce ordinary
weights without a starter allocation. Epoch collection gathers finalized history;
epoch republication refreshes the latest verified ordinary vector. It does not
recompute the daily score and does not require a starter guard.

Read [ordinary scoring and outage rules](ORDINARY_SCORING.md#publication-and-outages).
A previous day's receipt remains valid recovery evidence when a transaction was
submitted before the cutoff and included after midnight. Exact calculation,
identity, transaction and event checks remain mandatory. Recovery never authorizes
a new send after the 24-hour expiry.

During multi-day catch-up, every missed day's complete accounting is verified and
retained. Stale days are explicitly recorded as accounted but not published;
the latest completed day's fresh scores supply the next daily publication.
A catch-up record never substitutes for a finalized transaction receipt. Preserve
these records and their retained calculations in backups and restored state.

Reconstruct each completed day using activity and holding cohorts through that
day. Collecting a later day's receipts must not change an earlier day's first
activity, registration fallback or holding samples. Later receipts remain in the
ledger for their own days. The existing within-day sampling grid and completeness
checks still apply; a retry must not delete historical samples or replace retained
calculations to hide a mismatch.

Preserve publication intents and receipts across restarts. Resolve an uncertain
submission before sending again. See [publication recovery](runbooks/publication-status-unknown.md).
[Legacy epoch maintenance](LEGACY_EPOCH_MAINTENANCE.md) describes older modes,
including their separate starter guard; do not run it as a V5 requirement.
