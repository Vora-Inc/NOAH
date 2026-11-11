> Historical documentation. These are predecessor rules, not instructions for points-204040-v5. See [ordinary scoring](ORDINARY_SCORING.md).

# Daily scoring with epoch maintenance

This page documents the legacy non-starter maintenance path. Current Mainnet
uses the [starter profile](STARTER_ENTRY.md), both epoch flags set to `true`,
and an [independent guard](VALIDATOR_QUICKSTART.md#start-the-guard-and-validator).
The defaults below are not the current Mainnet setup.

Legacy maintenance keeps the daily scoring formula and UTC cutoff unchanged. Two
optional settings let an operator collect finalized history throughout the day
and refresh the latest completed daily weights each epoch. Both default to false.

```ini
NOAH_EPOCH_COLLECTION=false
NOAH_EPOCH_REPUBLICATION=false
```

Use `true` to enable a feature after reviewing and rehearsing the release.
Republication requires `NOAH_PUBLISH_MODE=direct`. No new wallet, key, cron job
or second validator process is needed. These options work in the existing
settings file and environment. The normal signer and publication permissions
still apply. Each additional publication pays a chain transaction fee.

This guide describes the legacy/default policy without starter entry enabled.
With the separate starter policy enabled, epoch publication builds a fresh
starter overlay and uses an independent fallback guard; the unchanged-vector
and new-miner timing rules below do not describe that route. See
[Starter entry](STARTER_ENTRY.md) for its allocation, eligibility and safeguards.

## What happens under the legacy/default policy

1. The existing daily loop runs first, under its existing writer lock.
2. Maintenance reads the finalized `LastEpochBlock` for the configured subnet.
3. Once per observed epoch, it can republish the latest successfully published
   completed daily vector. This does not recalculate scores or award new credit.
4. It collects the current UTC day's finalized deposits, emissions and collateral
   observations in chunks of at most 1,024 new blocks per loop pass. Holdings keep
   the same daily sampling grid. Interrupted chunks resume from their last
   completed block. A newly discovered wallet is sampled at earlier grid points.
5. After UTC day closure is finalized and settled, the normal daily calculation
   finishes the uncollected remainder,
   checks the complete day, runs both scoring implementations, and publishes the
   new daily vector. Partial collection never marks a day complete.

Collection advances once per observed epoch after catching up. It continues in
chunks on subsequent loop passes when behind. Timing follows the existing loop
poll interval, not a new timer. A slow node can still delay a pass; chunking is a
block-count limit, not a guaranteed time limit.

## Legacy/default publication safeguards

A refresh requires the retained calculation files and their hashes, the exact
finalized original daily transaction, the matching chain and subnet, unchanged
scoring dials and deposit destinations, and the original scored block hash. It
also checks the current miner hotkeys against the scored seat map. Every original
UID must still exist with the same hotkey, including originally zero-weight seats.
An additional unscored UID is allowed, but receives none of the saved vector's
weight. The exact saved vector is preserved. Missing or changed original seats
refuse a refresh rather than giving an old miner's score to its replacement.
Newly qualified miners enter through a fresh completed-day calculation.
The existing owner exclusion, signer route, validator permission checks, chain
rate limit, publication HOLD, shared send intent and failure backoff remain.

A fresh daily publication in the same epoch already satisfies that epoch. A
recent publication causes the next daily attempt to wait for the chain's rate
limit, rather than permanently refusing the day. Startup also reports this
specific condition as WAIT; other startup failures remain failures.

After UTC midnight, maintenance waits for the newly completed day's publication.
It never starts a new refresh of an older day's weights to disguise a failed daily run.
A transaction submitted before midnight can be included afterward. The next daily
publication recognizes that exact finalized refresh from its retained receipt,
original calculation and scoped chain events; it still requires a fresh daily
calculation. Unknown transactions, even in the same block as a known refresh,
remain a refusal. This recovery works after restart or with refreshes disabled.
When a send is uncertain, it reconciles the existing transaction. It does not
blindly resend. A persisted attempted-epoch marker prevents another send attempt
for that epoch after restart.

## Evidence and recovery

The existing daily score and submission files stay unchanged. Additional files,
relative to the existing submitted-record directory, are:

- `epoch-maintenance.json`: chain/subnet/validator identity, last observed work,
  collected height, publication outcome, and attempted/published epoch.
- `epoch-publications/<epoch>/day-<day>.submitted.json`: exact finalized refresh
  receipt referencing the original calculation evidence.

Logs with `[epoch]` report collection progress and failures. Existing daily
alerts and evidence remain necessary. A receipt proves publication, not the
resulting epoch emissions; inspect the completed epoch separately.

Collection adds two optional, strictly validated cache tables to the existing
history database: `epoch_collection` and `epoch_holding_cache`. Back up the full
SQLite database consistently, including these tables. Never restore only cache
rows over different history. No schema-version number is changed; older binaries
reject these extra tables. For a legacy non-starter deployment only, turning both flags off leaves daily
processing enabled. This is not a starter rollback: disabling required epoch
features with the starter snapshot configured is refused. A binary downgrade requires an
operator-reviewed database plan, such as a consistent pre-feature backup plus
history replay. Do not delete history, send intents, or submission receipts.

Before production, rehearse two epochs, a restart, an interrupted/uncertain send,
and UTC day close. Compare a full-history replay with progressive collection at
the same closed-day block: attributed receipts, earnings, collateral, holdings,
fixture, both engines' output and final vector must agree. Measure time and fees
on the target environment; local synthetic tests do not establish either.
