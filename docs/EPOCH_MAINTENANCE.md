# Daily scoring and epoch publication

> The current release candidate is V2.0, internally identified as `points-204040-v5` (policy/5). Historical policy identifiers remain unchanged for verification.

For the review candidate `points-204040-v5`, daily calculations produce ordinary
weights without a starter allocation. Epoch collection gathers finalized history;
epoch republication refreshes the latest verified ordinary vector. It does not
recompute the daily score and does not require a starter guard.

Read [ordinary scoring and outage rules](ORDINARY_SCORING.md#publication-and-outages).
A previous day's receipt remains valid recovery evidence when a transaction was
submitted before the cutoff and included after midnight. Exact calculation,
identity, transaction and event checks remain mandatory. Recovery never authorizes
a new send after the 24-hour expiry.

Preserve publication intents and receipts across restarts. Resolve an uncertain
submission before sending again. See [publication recovery](runbooks/publication-status-unknown.md).
[Legacy epoch maintenance](LEGACY_EPOCH_MAINTENANCE.md) describes older modes,
including their separate starter guard; do not run it as a V5 requirement.
