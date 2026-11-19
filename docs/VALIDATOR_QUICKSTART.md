# Validator: Legacy defaults and optional candidates

**Review candidate, not an instruction to switch a live validator.** Miners only
need registration and correctly attributed contributions; they do not run a
validator. An independent validator needs its own chain-authorized publishing
identity, two verified history readers, retained accounting evidence and a signer.
Software configuration does not grant a validator permit.

Read [Legacy scoring](LEGACY_SCORING.md) for the default mode and
[ordinary scoring](ORDINARY_SCORING.md) for the optional V2.0 candidate. Install with
`./scripts/install.sh`; `.env.validator.mainnet.example` is a placeholder example,
not a ready-to-run production file. Never commit a filled file or signing material.

## Shipping defaults

Both optional features ship OFF: `NOAH_SCORING_MODE=legacy`, empty
`NOAH_POINTS_POLICY`, `NOAH_REPORT_GATE_ENABLED=false`, and empty
`NOAH_REPORT_GATE_POLICY`. No feature is activated by installation or an offline
preview. The reserved report ON setting currently refuses startup before chain
access; authenticated production report enforcement is not implemented yet.
See [report commands and the dependency advisory](REPORTS.md).

Legacy retains its existing starter policy: preserve the verified original entry
snapshot, all financial history and the healthy starter guard. Do not fabricate a
new cutoff or replace deployed configuration with this placeholder example.
[Legacy validator setup](LEGACY_VALIDATOR_QUICKSTART.md) documents that flow;
its historical installation details must be read with the current installer.
Reports use a separate `.venv-reports` environment (Python 3.11+).

## Optional V2.0 selection — not the shipping default

The candidate selection below is for review, not activation:

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
receipts. The candidate refuses unresolved old publications. The old starter guard
must not be running beside the V2.0 ordinary publisher; this does not remove
the guard requirement while Legacy starter allocation remains selected. A code/config rollback after
new-mode financial publication is not a safe automatic recovery.

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
