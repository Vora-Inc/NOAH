# Validator setup and Mainnet scoring modes

Miners only
need registration and correctly attributed contributions; they do not run a
validator. An independent validator needs its own chain-authorized publishing
identity, two verified history readers, retained accounting evidence and a signer.
Software configuration does not grant a validator permit.

Read [Legacy scoring](LEGACY_SCORING.md) for the default mode and
[ordinary scoring](ORDINARY_SCORING.md) for the live Mainnet V2.1 rules and historical V2 behavior. Install with
`./scripts/install.sh`; `.env.validator.mainnet.example` is a placeholder example,
not a ready-to-run production file. Never commit a filled file or signing material.

## Shipping defaults

The release ships with `NOAH_SCORING_MODE=legacy`, empty `NOAH_POINTS_POLICY`,
`NOAH_REPORT_GATE_ENABLED=false`, empty `NOAH_REPORT_GATE_POLICY`,
`NOAH_WEEKLY_ASSIGNMENTS=false`, and `NOAH_BURN_ALLOCATION_BPS=0`.
Installation and offline previews do not activate these features. Authenticated
report processing is implemented through a separate report worker and a verified
report policy. Mainnet enabled V2, `NOAH_REPORT_REWARD=bonus-20`, weekly
assignments and temporary miner burn `NOAH_BURN_ALLOCATION_BPS=9800` on
November 20, 2026. From November 24, 2026 at 00:00 UTC it uses V2.1
(`points-204040-v6`) with those reporting and burn settings. See [report configuration and commands](REPORTS.md).

Legacy retains its existing starter policy: preserve the verified original entry
snapshot, all financial history and the healthy starter guard. Do not fabricate a
new cutoff or replace deployed configuration with this placeholder example.
[Legacy validator setup](LEGACY_VALIDATOR_QUICKSTART.md) documents that flow;
its historical installation details must be read with the current installer.
Reports use a separate `.venv-reports` environment (Python 3.11+).

## V2.0 settings for historical reference

The V2.0 settings below document the predecessor required by a verified V2.1
migration. First migration requires the maintained held transitions
Legacy → V3 → EMA V4 → V5, preserving financial state and predecessor evidence.
Do not start intermediate modes or try to create the required policy by editing
these settings alone:

```dotenv
NOAH_SCORING_MODE=points-204040-v5
NOAH_PARAMETERS_JSON=/path/to/docs/parameters/profiles/mainnet-production-points-204040-v5.json
NOAH_POINTS_POLICY=/path/to/verified-ema-policy.json
NOAH_STARTER_ENTRY_SNAPSHOT=/path/to/preserved-entry-snapshot.json
NOAH_PUBLISH_MODE=direct
NOAH_EPOCH_COLLECTION=true
NOAH_EPOCH_REPUBLICATION=true
NOAH_REPORT_GATE_ENABLED=false
NOAH_REPORT_GATE_POLICY=
```

In V2.0, `NOAH_STARTER_ENTRY_SNAPSHOT` is the historical entry-accounting file,
despite its old name. It does not select a pool in that mode. Preserve the pinned EMA policy and predecessor
archives, original cutoff, consumed receipts and all financial balances. Do not
create a new cutoff to make an installation pass. The new profile's retained
legacy fields support historical replay; the V5 points rules live in
`noah/points/onboarding.py`, not the legacy decay/ramp fields in that profile.

Before switching, reconcile pending old-mode sends and any active old overlay
under the predecessor configuration. Preserve journals, backups and recovery
receipts. The validator refuses unresolved old publications. The old starter guard
must not be running beside the V2.0 ordinary publisher; this does not remove
the guard requirement while Legacy starter allocation remains selected. Once a
new financial policy has been installed, changing configuration or selecting an
older executable is not a safe way to return to Legacy, even before the first V5
publication. Recovery must preserve policy history, balances and consumed receipts;
disabling optional reporting or burn does not undo that migration.

A configured read-only preflight (requires the named readers and evidence) is:

```sh
.venv-validator/bin/python -m noah.points.preflight --env /path/to/validator.env
```

This is not permission to sign, nor evidence that a new installation is qualified.
The exact release still needs chain rehearsal, finalized publication/readback,
reward verification, interruption/recovery, outage expiry, midnight inclusion,
backup restoration and a verified foreign receipt before a live release decision.

Older installation details are retained in [legacy validator setup](LEGACY_VALIDATOR_QUICKSTART.md)
for reproducing those versions. Its old profile and guard commands are not V5 setup.
Use [score verification](VERIFYING_SCORES.md) to check actual retained evidence.
