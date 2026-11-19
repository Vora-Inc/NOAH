# Miner reports and validator checks

This candidate includes report commands and an optional daily reporting bonus.
The proposed rollout combines V2 scoring, reporting bonuses and temporary miner
burn; this guide is not an activation announcement. Shipping defaults remain
Legacy scoring, reports OFF and burn OFF. Mainnet report submission requires an
explicit per-command opt-in after activation approval.

## What reporting earns

In the proposed `bonus-20` mode, an accurate, timely report can add up to 20
points to the miner's daily payout calculation. Missing, late, invalid or
inaccurate reports earn no bonus and leave the ordinary V2 score intact. A
verified unresolved verdict also earns no bonus; a broken reader, worker or
evidence package can still stop validator publication rather than authorize an
unverified result.

The V2 bonus is `20 × maturity`: at full maturity it is 20 points; at 1%
maturity it is 0.2 points. Existing debt rules halve it on the first uncovered
debt day and remove it from the second. Ordinary eligibility still applies.
It is at most one bonus per scoring day, not 20 points per submitted report.
The bonus is an allocation input, not a token payment, deposit, or maturity
credit. Rewards depend on other miners' scores and the final published weights.

Reporting and deposit validation remain separate. An accurate report does not
pay debt or replace an ordinary contribution. Keep using the deposit commands
for contributions and the report commands for this optional work.

The proposed temporary `9800` burn setting allocates approximately 98% of the
miner weight distribution to a verified burn destination, after adding report
bonuses. Reporting therefore affects a miner's share of the remaining allocation;
it does not exempt a bonus from burn. Integer rounding, minimum weight units and
consensus affect the realized share. This is not 98% of all subnet emissions:
owner cut and validator dividends are separate. Burn OFF leaves scoring and
reporting settings unchanged; its exit requires a confirmed replacement
publication, not merely editing a setting.

## What a miner reports

An operator distributes a frozen `noah.backing.round.v1` JSON document naming a
chain, finalized snapshot block B, reported subnet, Ark destination, calculation
version, release identity and commit/reveal windows. The mining subnet is explicit
in the round and must match the miner configuration (82 on Mainnet; 2 on canonical
Testnet). The reported subnet may be a different non-root subnet. Native versus
foreign deposit accounting remains the responsibility of the ordinary scorer.

For each assigned subnet, miners calculate three figures at the specified snapshot.

1. Price drop in parts per million if the largest available non-Ark holder sells.
2. Smallest sale causing at least a 10% price drop, in alpha rao.
3. Smallest sale causing at least a 20% price drop, in alpha rao.

For example, `drop_ppm=10000` means a 1% drop. A threshold of
`5000000000` means 5 alpha. These numbers illustrate the units; they are not a
report to submit. Threshold searches use steps of 1,000,000 rao (0.001 alpha),
so accuracy means exact equality to the pinned integer algorithm, including its
step size and rounding. Floating-point estimates are not accepted.

Report validation checks accuracy and the miner's registered identity at B.
Deposit validation separately applies the existing deposit/scoring rules.
Passing the report check does not replace those rules. There is no additional
prior-deposit requirement or deposit-history scan
in the report-only policy `registered-at-b-accurate-report-v1`.

The chain receives a hash commitment followed by its exact203-byte reveal via
`Commitments.set_commitment`. There is no new HTTP intake API or chain pallet.
The new wire domain is `noah-rpt-v1`; it commits both subnet IDs and contains no
deposit reference. Historical207-byte `bfpoc-s2-v2` reports retain their original
deposit policy and cannot be reinterpreted as the new policy. Application code
lives in `noah.reports` and has no prototype-directory dependency.

## Install

From the public repository root:

```sh
./scripts/install.sh
source .venv-validator/bin/activate
noah-miner --help
noah-validator --help
```

Reports require Python 3.11+ and use `.venv-reports`. The synchronous report
decoder depends on `substrate-interface`/`scalecodec`; the validator SDK uses
`async-substrate-interface`/`cyscale`. These distributions must stay isolated.
`./scripts/install-reports.sh` installs just the report worker; its console
commands can prepare/submit reports and run offline checks without Bittensor.
Running the continuous validator requires `.venv-validator`.

The front-end commands launch the installed worker with an isolated Python
module path. `NOAH_REPORT_PYTHON` explicitly selects the installed worker when
live report processing is enabled.
Keep it on the same reviewed source revision. Node 24+ is needed for twin-scored
previews. No separate report package or prototype checkout is installed.

## Miner commands

Reuse the existing `noah-deposit.ini` wallet names, public identities, endpoint,
genesis, subnet, Ark destination and state directory. Reporting always signs
with the miner's configured **SR25519 hotkey**; the deposit coldkey/proxy signer is not
used. Do not put seeds or passwords in the configuration or round document.

```sh
noah-miner --config noah-deposit.ini report prepare \
  --round round.json
noah-miner --config noah-deposit.ini report status --round round.json
noah-miner --config noah-deposit.ini report submit --round round.json --follow
```

Preparation reads the pinned state, checks the finalized snapshot and configured miner
owner/registration, and saves one immutable payload and salt. Repeated preparation
reuses that payload. Submission prompts for `SUBMIT`; an explicitly authorized
scheduled run can use `--yes`. `--follow` waits for the frozen windows and starts
no further tick once `--timeout-seconds` expires (default five hours). An in-flight
tick, including its RPC or transaction confirmation, is not interrupted by this
timeout. Timeout exits with status 1 and preserves the journals; it does not prove
that an in-flight transaction was unsent. Inspect and reconcile retained state
before retrying. Without `--follow`, submission
advances at most one phase, which makes it usable as a scheduler tick.

Mainnet submission additionally requires `--allow-mainnet-report`. This explicit
opt-in only accepts the new report-only policy on mining subnet82, and does not
override chain identity or window checks. It does not enable validator report
processing or V2 scoring. Do not add it before the approved report activation.

Commit requires registration and the correct chain; reveal requires the finalized
commit. Each new phase checks the current finalized runtime against the supported
reader policy before opening the wallet, and the signing adapter checks again
before building the extrinsic. At the actual signature request it also binds the
SDK-selected runtime, transaction version and composed metadata; the SDK may use
an unfinalized best head. Preparation's old snapshot check alone is insufficient.
A proven refusal before signing is recorded as failed with no submission attempted;
that phase is not automatically retried, but it does not lock out later rounds.
An error after signing begins remains uncertain. Existing journal reconciliation
remains available without authorizing a new signature. Waiting, status and uncertain-send reconciliation do not unlock a wallet.

Live report SDK requests use a 30-second monotonic socket-I/O deadline, including
subscription confirmation. Partial WebSocket frames, unrelated messages and HTTP
response header/body fragments do not restart that deadline. Connection setup also
receives explicit timeouts; operating-system DNS resolution and underlying
TCP/TLS/proxy setup and CPU parsing are not forcibly interrupted by this Python
guard; expiry is checked when control returns. HTTP redirects refuse rather than
replaying RPC POSTs. These are request bounds, not a hard wall-clock limit for an entire CLI tick or preparation.
A timeout closes that SDK connection. A submission whose acknowledgement timed
out remains unresolved with its original extrinsic hash; the client does not
reconnect/re-send it automatically. Reconcile the original journal before any
subsequent action. Offline replay and the validator's separately budgeted evidence
collector retain their existing transports.

A crash/unknown send never causes automatic re-signing or rebroadcast. Rerun the
same command against the same state; recovery searches finalized history back to
B, including transactions older than 64 blocks. This requires archive access and
can take time. Preserve the entire state directory, especially `prepared.json`
and both journals. Losing the salt makes the reveal impossible. Never delete a
journal to force a retry.

A per-miner/subnet lock prevents concurrent rounds from overwriting the same
commitment slot. An unresolved prior send blocks another round. A prior finalized
commit whose reveal was never submitted releases that block only after its reveal
window closes; its missed report remains recorded. Chain inclusion near a deadline
can still be late even if submission began before it. Validator verification uses
the actual finalized inclusion timestamp. Fees and chain commitment rate limits
still apply; commands do not bypass them.

## Daily reporting from a fixed schedule

An operator can distribute one immutable `noah.report-schedule/1` document
instead of distributing a new round every day. It pins the chain, both subnet
IDs, Ark identities, calculation/release versions, day anchor, effective day,
snapshot offset, preparation lead and two-hour commit/two-hour reveal windows.
The snapshot is the first finalized block at or after the daily snapshot time;
its predecessor must be before that time. A delayed snapshot cannot move the
windows. A miner may have only one outstanding report per mining subnet.

Boundary discovery searches backward from the finalized head to bracket the
snapshot time, then finds the exact first block within that bracket. Readers
must retain the historical state needed for every bracket probe, the snapshot
and its predecessor, as well as the report collection window. Missing state
refuses resolution; it is never treated as a timestamp before the snapshot.
The weekly assignment issuer uses the same search for the Sunday roster.
This avoids requiring ancient half-chain state just to resolve a recent day
or week; it does not remove the need to retain the probed bracket history.
The existing request, time and byte limits remain in force.

New resolutions retain `noah.report-schedule-boundary/2` proofs. Existing `/1`
proofs replay with their original query sequence, including cached miner
resolutions and retained validator evidence. The proof version does not change
the schedule identity, round, report calculation or reward. A frozen request
keeps its original financial source and bytes; changing that source or an
unresolved publication intent still refuses a retry.

```sh
noah-miner --config noah-deposit.ini report run --schedule report-schedule.json
noah-miner --config noah-deposit.ini report run --schedule report-schedule.json --yes --follow
```

The first command performs one tick; transaction submission requires interactive
confirmation or `--yes`. `--follow` starts no further tick after five hours by
default; as with `submit`, an in-flight tick may finish after that deadline.
It does not install a service or enable future unattended runs. Retained daily
boundary proofs, prepared payloads and transaction journals survive restarts.
An uncertain old send is reconciled before a new daily round is prepared, without
opening the wallet again. Existing deposit commands remain separate.

For a supervised trial of one already prepared round, use `submit --round`
with `--follow --timeout-seconds SECONDS` to keep the round fixed. Start the
process before its required window and retain its output and exit status.
The process must remain running; a chat reminder is not a submission scheduler.
Use `status --round` and retained receipts to check completion. If the process
stops or the outcome is uncertain, resume the same saved round and journals;
do not delete them or generate a replacement payload to force a retry.

Validators resolve the same schedule using independent readers and can replay
the retained boundary proof offline:

```sh
noah-validator reports resolve-day --invocation private-schedule-invocation.json
noah-validator reports verify-resolution --schedule report-schedule.json \
  --day 123 --resolution retained-resolution.json
```

The private invocation has exactly `schedule` (the complete document), `day`
(the integer day relative to its anchor), `endpoints` (two independent reader
URLs) and `require_pair: true`. Keep credentials in this private file. Capture
the JSON result as the retained resolution. Offline verification requires paired
agreement. Resolving a day does not enable report processing or V2 scoring.

## Weekly assignments

Weekly reporting is OFF by default. The proposed Testnet mode preserves ordinary
V2 pay when a report is absent or inaccurate. All three assigned reports must
verify to earn the one daily bonus of up to 20 points, subject to existing maturity
and debt rules. Deposit validation remains separate. This configuration is not
approval to activate Mainnet.

The validator automatically publishes the Sunday file for the finalized UTC week
and catches up after downtime. It retains a valid file unchanged across retries
and restarts. Operator configuration selects V2 scoring and these report settings:

```sh
NOAH_REPORT_GATE_ENABLED=true
NOAH_WEEKLY_ASSIGNMENTS=true
NOAH_REPORT_GATE_POLICY=/var/lib/noah-validator/week-template.json
NOAH_REPORT_REWARD=bonus-20
NOAH_WEEKLY_ASSIGNMENT_SIGNING_KEY_FILE=/private/path/assignment-signing-key
```

The trusted `noah.weekly-assignment-template/2` names
`assignment_signer_public_key` (64 lowercase hex characters) and
`commitment_format: "context-v1"`. Use a separate assignment-only SR25519 key;
the validator hotkey stays in its existing custody setup. The signing key never
belongs in the template, miner INI, downloaded file, or public repository.
Signatures are domain-bound to weekly assignment files. The downloaded file
cannot select a new trusted key. Key rotation requires a separate operator change.

The file is written to
`<submitted-record-dir>/report-state/weekly-assignments/<Sunday>.json`.
Serve that directory at the configured read-only assignment URL. Writing the
file does not host it. A supervised write is also available:

```sh
noah-validator reports publish-week --template week-template.json \
  --endpoint "$REPORT_RPC" --out weekly-assignments --week 2026-11-22 \
  --signing-key-file /private/path/assignment-signing-key
```

Each row binds the miner's hotkey, coldkey and registration block at week open.
Miners receive up to three distinct supported subnets. Fewer than three targets
can still produce observations but cannot earn the bonus. A newly registered or
replaced miner waits for the next Sunday roster.

Configure the URL and obtain the trusted public key independently from the
operator, once. The miner's existing genesis and Ark identity settings must also
match the signed file:

```ini
[report]
assignments_url = https://assignments.example/weekly-assignments/
assignment_signer_public_key = OPERATOR_PROVIDED_64_LOWERCASE_HEX_PUBLIC_KEY
```

A directory URL appends `<Sunday>.json` using finalized chain time. A fixed-file
URL must serve the current week. The miner verifies signer, network and week
before retaining a file. During a URL outage it can use an authenticated cache
for the current week; it never falls back to a previous week.

Run once each day:

```sh
noah-miner --config noah-deposit.ini report assignments --yes --follow
```

Without `--follow`, each invocation advances at most one target phase.
Without a configured URL or explicit `--file`, this command reports `disabled`.
`--file` still requires the configured signer and current week. `--follow` waits
for the fixed windows and follows all targets, refreshing the file if the UTC
week changes. Its timeout preserves prepared reports and phase journals. Rerun
the same command to resume; uncertain sends are read back without authorizing a
replacement. A terminal failed target does not prevent other targets completing.
`complete` means the local reveals finalized, not that a validator awarded points.

New rounds explicitly select `context-v1`: the 79-byte commitment binds the
mining subnet, reported subnet, full round ID and complete report preimage.
The first valid commitment for each miner/subnet/round stands, even if its reveal
is missing. Missing one reveal affects only that report; other verified results
remain intact, and ordinary V2 pay continues without the bonus. Report-only
reveals remain 203 bytes. Historical absent-format rounds retain their original
Raw32 commitment and 203-byte report-only or 207-byte deposit-report verification.
See [the wire protocol](REPORT_COMMITMENT_PROTOCOL.md).

Testnet acceptance requires actual hosted-file fetch, all three on-chain reports
with the bonus through V2 pay, an omitted reveal with two preserved verified
results and no bonus, and rejection of a different assignment signer on both
sides. Local fixtures do not establish live acceptance. The shared-window package
passed synthetic capacity and fresh replay checks for 128 targets, 214 miners and
7,200 blocks under the existing limits. Final Linux release checks and actual
host qualification remain required before claiming production readiness.

## Validator commands

Obtain the round and miner roster from trusted operator configuration. The roster
is a JSON list of public SS58 hotkeys or raw AccountId hex strings. The round
must match the package when it is verified.

```sh
noah-validator reports collect --round round.json --endpoint "$REPORT_RPC" \
  --hotkeys hotkeys.json --out report-evidence
noah-validator reports verify --round round.json --package "$REPORT_PACKAGE"
noah-validator reports status --evidence report-evidence
```

Report-only collection covers B through the finalized cutoff and rejects
`--history` and `--allow-genesis-scan`. The `history` command remains available
for historical deposit-policy evidence; it is not part of current reporting.

`reports watch` accepts the same inputs as `collect`, plus `--poll-seconds` and
`--timeout-seconds`. It repeats collection and reconstruction until the round is
resolved or the time limit is reached. No system service or timer is installed.
`collection_tick` and `history_tick` in `noah.reports.validator` are the shared
Python APIs for a future scheduler; the CLI uses those same functions.

Packages retain raw RPC responses, calculation receipts, metadata and portable
history segments for historical policies only. `verify` disables network connections in its worker and
rebuilds the result. It does not trust saved verdict JSON. Multiple `--endpoint`
arguments provide **failover**, not independent agreement. Replay proves what the
retained responses imply; it does not prove an RPC provider was honest. This
collector alone is not sufficient evidence for a live reward or penalty.

The paired provider obtains the complete registered roster itself, authenticates
scored identities including owner and registration block, freezes one finalized
evaluation boundary, and reconstructs both readers before deciding:

```sh
noah-validator reports adjudicate --invocation invocation.json --output new-private-package
noah-validator reports adjudicate-verify --request semantic-request.json \
  --package new-private-package --manifest-sha256 "$MANIFEST_SHA256"
```

The invocation contains the frozen semantic request and two independent reader
connections. History is null for both. The offline command requires the exact
request and externally pinned root manifest hash. A valid result has status
COMPLETE or UNRESOLVED; unresolved is not verified accuracy. Missing/corrupt
proof, wrong identities or a different request cause a nonzero exit. A caller
must not silently substitute an unfiltered score after such a failure.

New packages also retain the full calculation as `engine.json` and its round and
calculation-projection binding as `engine-binding.json`. Both use sorted, compact
ASCII JSON preserving integer types, with a final newline. The engine digest is
SHA-256 of those exact bytes; it is distinct from the round/projection digests.
Offline reconstruction compares these artifacts against the calculation rebuilt
from raw responses. A changed, partial or symlinked artifact refuses verification.
Legacy packages with neither file remain replayable but explicitly report
`engine_retained: false`; they do not satisfy the new publication contract.
These files alone do not supply a final resolved verdict roster or authorize
website publication.
An unavailable threshold result is retained with a null comparison projection.
If the reference calculation or any required output is unusable, the checker
marks the round unresolved before assessing missing or invalid miner submissions;
miners cannot prepare an accurate payload against an unavailable reference.
The internal collector can also freeze a finalized evaluation block and retain
its ancestry/cutoff proof. Offline verification reconstructs that proof before
returning verdicts. Ordinary CLI collection does not yet select a frozen boundary;
the independent provider integration must supply the exact trusted evaluation.

## Prepare a website report after approved activation

`export-public` reconstructs a pinned paired-reader package with network access
blocked, then prepares one sanitized JSON file and a separate private provenance
receipt. It requires an operator-approved Mainnet activation receipt pinned by
SHA-256, a fully resolved completed Mainnet round and its complete authenticated
snapshot roster. Testnet packages are refused; they cannot be relabeled.

For weekly assignments, public required/verified/missing counts cover only miners
assigned to that subnet, matched by hotkey, coldkey and registration incarnation
in the authenticated request. An unrelated unassigned miner does not make a
fully reported subnet look incomplete. The exporter still authenticates the
complete snapshot roster and retains the full verdict evidence hash. Historical
packages without weekly assignments retain their original full-roster counts.
The exporter conservatively requires the complete round to resolve before
exporting, including unassigned observations; the display filter does not waive
that evidence check. The publisher must supply the trusted validator-generated
request and externally approved manifest hash. The exporter authenticates their
pinned bytes and replays report evidence; weekly signature and selection-authority
checks belong to the validator. It does not independently repeat those checks.

```sh
noah-validator reports export-public --request semantic-request.json \
  --package private-package --manifest-sha256 "$MANIFEST_SHA256" \
  --round-sha256 "$ROUND_SHA256" --activation-receipt approved-activation.json \
  --activation-receipt-sha256 "$APPROVAL_SHA256" \
  --published-at 2026-11-16T06:00:00Z --out public-reports \
  --receipt private-exports/round-receipt.json
```

The date above is an example, not activation approval. Supply the real explicit
UTC timestamp and preserve it for retries. The activation receipt is a pinned
operator attestation, not automatic evidence of a running validator setting.
The output contains only allowlisted aggregate figures and provenance hashes;
it excludes identities, deposits, credentials, endpoints and private paths.

The private receipt binds request, policy, round, approval, package manifest,
exporter source and exact public-file hashes. Keep it outside both the public
output and the pinned evidence package. Its `VERIFIED_EXPORT_INTENT` state means
verification completed before writing the public file; an interruption may leave
only that intent. Repeating the exact command verifies again and safely completes
the same bytes. `PREPARED` is returned only after both files exist. Corrections
require `--supersedes` and a new private receipt path; existing public files are
never overwritten. The command does not upload, edit the website's approved
index or enable either feature toggle.

Before publication, independently validate the prepared file using the website
repository's `node tools/validate-backing-export.mjs FILE.json [HISTORY.json ...]`.
Publishing approved JSON and activating website display are separate operator
steps after approved report activation. The proposed combined V2/bonus/burn
rollout does not automatically publish JSON or enable website display.

## Validator settings and compatibility

V2 scoring, report processing and burn are separate settings. The proposed
combination selects `NOAH_SCORING_MODE=points-204040-v5`, enables
`NOAH_REPORT_GATE_ENABLED=true`, explicitly sets `NOAH_REPORT_REWARD=bonus-20`,
and sets `NOAH_BURN_ALLOCATION_BPS=9800`. These settings are for a separately
qualified and approved activation; they are not shipping defaults or an
instruction to enable an installed validator.
Nonzero burn requires V2; the accepted burn values are only `0` and `9800`.
Burn activation also requires an explicit on-chain `RecycleOrBurn=Burn` value.
The validator reads the actual storage at a finalized block; an absent value
does not qualify even if an SDK supplies the runtime's default `Burn` value.

The name `NOAH_REPORT_GATE_ENABLED` is retained for compatibility. The reward
selector matters: its default `gate` preserves the older penalty behavior.
Enabling reports without explicitly selecting `bonus-20` does **not** select
the optional bonus policy. A state directory previously activated in `gate`
mode cannot be silently reused as `bonus-20`; preserve its activation records,
financial history and pending intents through a reviewed transition.

### Preserving an existing penalty-mode activation

The candidate supports one prospective transition from `gate` to `bonus-20`.
It retains the original activation document and selects a future scoring day
as the boundary. Days before that boundary keep their original gate policy;
days at or after it use the bonus policy. Historical requests, proofs, credits
and publication receipts are preserved: replaying them does not award a new
bonus or rewrite their previous result.

The transition records the announcement configuration for provenance. It does
not freeze that schedule forever or change the existing schedule-rotation
rules. It also does not change the scoring calendar, report protocol, V2
ledger, or contribution obligation.

An operator must prepare and review this transition with the validator and
publication guard stopped, the host on HOLD, and the existing release and
publication locks held. Unresolved publication/release operations must be
reconciled first; a conflicting boundary or already-frozen day at or beyond
the proposed boundary is refused. Writing the transition does not edit the
environment, restart services, send a transaction, or enable the bonus: the
operator must separately select the reviewed `bonus-20` configuration and
complete the approved release procedure.

Verify that the validator service can read the prepared transition record.
Applying it requires exact-artifact qualification and separately approved
deployment; preparing the record does not authorize Testnet or Mainnet activation.

Report processing requires a reviewed frozen `NOAH_REPORT_GATE_POLICY` and
`NOAH_REPORT_PYTHON` selecting the installed isolated worker at the same source.
It uses direct publication, independent reader evidence and retained calculation
and recovery proofs. Startup checks network, mining subnet, activation boundary
and worker identity before wallet loading. With reports OFF, report-specific
startup probes do not run. With burn OFF, the reporting selection is unchanged.

## Historical penalty preview

```sh
noah-validator score --fixture scoring-fixture.json --out ungated-preview.json
noah-validator score --fixture scoring-fixture.json \
  --require-accurate-miner-report --context report-context.json \
  --rounds required-rounds.json --evidence report-evidence --out gated-preview.json
```

This compatibility preview models the older penalty gate, **not** the proposed
`bonus-20` policy. Its flag is **off by default** and independent of V2 selection.
These commands are offline previews; every result says `publishable: false`.
They run the real Python and TypeScript scorers, and refuse disagreement.

- Off: the scorer's result and payout vector are unchanged.
- On: preserve raw scores, points/history and raw pay weights. Confirmed missing,
  late, invalid or inaccurate reports zero the affected miner's effective payout
  weight for the scoring day containing the reveal cutoff. Remaining effective
  weights are renormalized to 65,535 units.
- Unresolved reader/history/evidence failures preserve the raw weight in the
  preview and remain explicitly unresolved. A stale prior round does not establish
  eligibility for a new round. Miners registered after B are not required for that
  round. No selected round is unresolved, not an invented success.
- If every effective weight is zero, produce no new vector. This does **not** erase
  weights already on chain and does not authorize an equal-weight fallback.

`required-rounds.json` is a nonempty JSON list of trusted round documents.
The diagnostic context contains `genesis_hash` (bare lowercase hex), `netuid`,
`day_anchor_ts`, `evaluated_at_ms`, `seats` (UID strings to bare hotkey hex), and
`registration_blocks` (snapshot block strings to UID/registration-block maps).
These are preview inputs, not an authenticated production chain-reader interface.

`NOAH_BACKING_SCORE_GATE` is an offline penalty-preview option, not a live
validator setting or a preview of the reporting bonus. Legacy/report
compatibility remains available; it does not change the proposed combined
V2/bonus/burn rollout or authorize activation.

## Test and review boundaries

```sh
./scripts/run-report-tests.sh
./scripts/run-tests.sh
```

The report suite covers integer math, snapshots, immutable preparation, restart,
unknown sends, concurrency, wrong identities, explicit windows, reconstruction,
history corruption and the independent scoring toggle. The full runner includes
that suite in its isolated third Python environment. Tests using synthetic or
retained responses are not fresh live-chain qualification. Current protocol
support includes runtime spec 490 and the exact official runtime-483 and
runtime-491 and runtime-492 code hashes. The 483 adapter reads `NetworksAdded`
for existence; 490, 491 and 492 use `SubnetState`. A different 483, 491 or 492
code hash is refused.
Unsupported runtime behavior fails closed. Runtime compatibility does not
enable reporting or change the separate activation settings.

The public package retains synthetic and oracle-vector regression suites. The
exact-block SN82 raw-response corpus and its dedicated replay test are excluded
because their records expose operational identities. Their private regression
result is not a public reproducibility claim. Public tests still run every
shipped report suite; no blanket skip replaces the omitted exact-chain case.

## Dependency advisory under review

The pinned synchronous decoder pulls in `ecdsa==0.19.2`.
[GHSA-wj6h-64fc-37mp](https://github.com/advisories/GHSA-wj6h-64fc-37mp)
reports an unpatched timing issue in ECDSA signing/key generation. Report signing
rejects every crypto type except SR25519 before claiming a journal or signing;
validator report commands do not sign. This removes that algorithm from the
supported report-signing path, but does not patch the dependency. The raw audit
remains failing and is retained for review; no blanket advisory suppression was
added. Replacing the legacy decoder or approving a narrowly justified exception
is required before calling the candidate release-ready.
