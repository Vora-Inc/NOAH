# Validator guide

The Noah Validator reads chain history, calculates miner scores, checks the
Python and TypeScript results, and publishes weights. Its local history store
preserves the records needed to score earlier earnings and contributions.

This edition includes the mainnet startup checks, the production scoring
profile, and a mainnet configuration example. Run the checks below and
review chain readbacks before starting an unattended service. The Ark
identities are listed in the [mainnet address reference](reference/WALLET_ADDRESSES.md).

## Install for review

You need a Unix-like system with Bash, Git, Python 3.10 or newer, Node.js 24
or newer, and npm. The setup has been used on macOS and Linux. Native Windows
and WSL have not been verified for this edition.

```bash
git clone https://github.com/Vora-Inc/NOAH.git
cd NOAH
```

Install from the default branch:

```bash
git switch main
./scripts/install.sh
```

The installer creates two Python environments:

| Environment | Purpose |
|---|---|
| `.venv` | Development and computation tests without the Bittensor SDK |
| `.venv-validator` | Validator tests with the Bittensor SDK and chain libraries |

It also installs the TypeScript verification dependencies. Successful
installation prints `Done.` The absence of the Bittensor SDK
from one environment is a tested import boundary, not a general guarantee
that arbitrary software in that environment cannot sign or send transactions.

Dependency requirements are in [pyproject.toml](../pyproject.toml) and the
[TypeScript package](../verification/noah-ts/package.json). Several Python
dependencies use version ranges; they are not all exact pins.

## Run the checks

```bash
./scripts/run-tests.sh
```

This runs both Python environments, TypeScript tests, a TypeScript type check,
and the shared-fixture comparison. Success ends with `All green.` Read the
summary for failures and skipped tests. Some checks require evidence not
bundled with this edition and may be skipped; that is not live-chain proof.

The [testing guide](TESTING.md) explains the scope of these results; the
[contribution guide](../CONTRIBUTING.md) explains how to propose changes.

## Configuration and startup

The entry point is `neurons/validator.py`. The example file below documents
its settings and distinguishes the two validator roles.

```bash
cp .env.validator.mainnet.example .env.validator
```

This creates an ignored local settings file. The example already names
mainnet, subnet 82, and the shared Ark addresses. Replace its `REPLACE-ME`
entries with the authorized scoring identity, dedicated proxy, connections and
storage paths for your installation.
An unfinished example is refused. Check the following settings:

| Setting | Purpose |
|---|---|
| `NOAH_NETWORK`, `NOAH_NETUID` | Intended network and subnet |
| `NOAH_CHAIN_ENDPOINT`, `NOAH_ENDPOINT_2` or `NOAH_ENDPOINT_2_FILE` | Two independently sourced chain connections; the file option keeps a keyed second address outside the settings file |
| `NOAH_WALLET_NAME`, `NOAH_WALLET_HOTKEY` | Local proxy wallet and key labels |
| `NOAH_EXPECTED_SIGNER_ADDRESS` | Your registered publishing hotkey that owns the seat and publishes weights |
| `NOAH_PROXY_SIGNER_ADDRESS` | Separate operational key signing the transaction |
| `NOAH_PUBLISH_MODE` | `direct` for the selected Null policy; uses the configured proxy and requires commit-reveal off |
| `NOAH_SUBMIT_ENDPOINT` | Submission connection; empty uses the primary endpoint |
| `NOAH_ARK_ADDRESS` | Ark receiving coldkey |
| `NOAH_VALIDATOR_HOTKEY` | Designated Ark Validator hotkey for qualifying deposits |
| `NOAH_DIALS_JSON`, `NOAH_PARAMETERS_JSON` | Approved scoring and eligibility configuration |
| `NOAH_ENTRY_DEPOSIT_RAO` | Entry amount matching the parameters profile |
| `NOAH_HOLDING_STRIDE_BLOCKS` | Interval between holding samples |
| `NOAH_HISTORY_DB`, `NOAH_RUNTIME_ROOT` | Durable history, progress, and score evidence locations |

The Noah validator identity and `NOAH_VALIDATOR_HOTKEY` serve different
roles. The latter identifies the Ark Validator, the required delegation
destination of Ark deposits.

Set `NOAH_EXPECTED_SIGNER_ADDRESS` to **your own registered publishing hotkey**
and `NOAH_PROXY_SIGNER_ADDRESS` to its separate authorized proxy. Replace the
`REPLACE-ME` values in the supplied example with these public addresses.
Publishing is not restricted to Noah's owner identity.
Keep the shared Ark receiving address and designated Ark Validator unchanged:
these select qualifying deposits, not the operator publishing the scores.

**Null still elects exactly one publishing permit holder**, the registered UID
with the highest stake weight. Configuring a different identity does not grant
that permit. Startup refuses publication without it, even for the subnet owner.
Other operators can reproduce scores with the [verification tools](VERIFYING_SCORES.md);
this change does not enable simultaneous publishers or switch consensus to Yuma.

Use separate history, runtime and publication-ledger paths for each publisher.
Do not reuse another publisher's pending attempts or submitted-day records.
Reconstruct the complete required finalized earnings and deposit history using
independent chain sources and the same scoring profile and UTC cutoff. An empty
history database or a successful startup check alone does not prove score parity.
Before enabling a new publisher, compare its results to retained score evidence.

`NOAH_WALLET_NAME` and `NOAH_WALLET_HOTKEY` are labels for the signing wallet
installed on the validator host. They are not recovery words or a way to
connect a Ledger. Automatic publication needs an available signing hotkey.
Never import hardware-wallet recovery words or a funds-controlling coldkey
to make the validator run.

## Automatic signing through the proxy

The proxy route is implemented in this edition. The validator identity stays
on its hardware wallet. An authorized, separate software key signs each
publication on its behalf; no daily hardware approval is required after the
proxy grant is in place.

The grant must be `NonFungible` with delay 0. The application checks that grant,
the loaded key, and the public identities before publication. Do not create a
broader grant to get past a refusal. The application only constructs approved
weight-publication calls; that application restriction does not reduce what a
stolen proxy key could do through the chain's wider NonFungible permission.
Protect the operational key and retain hardware access to revoke its grant.

The shipped implementation requires at least **0.1 TAO free in the proxy
account** before attempting publication. This is a software balance floor,
not a quoted fee, a daily expense, or an instruction to add another 0.1 TAO.
A successful chain call may waive its fee; a failed call can still cost fees.
Use the actual receipt to establish what was charged.

Keep the proxy's recovery material offline. Never export the hardware-held
validator key or a funds-controlling coldkey onto the host. Neither the key
nor recovery words belong in this repository or its environment files.

A successful transaction wrapper is not enough. The application requires
`ProxyExecuted` to report success for the inner weights call; missing evidence
leaves the outcome unknown. Publication records retain the Noah validator as
the commit identity and the proxy as the transaction signer.

Local tests exercise this path. Verify each production publication against
finalized chain records. Sealed mode additionally requires reveal verification;
the selected direct mode has no reveal step.

## Prepare the scoring configuration

Generate the scoring settings from the included profile rather than copying
values by hand. Choose an output location outside the source checkout:

```bash
NOAH_PARAMETERS_JSON="$PWD/docs/parameters/profiles/mainnet-production-v1.json" \
  .venv-validator/bin/python scripts/mainnet_dials_from_profile.py
```

This prints the seven settings as JSON without connecting to the chain. Save
that output to the location used by `NOAH_DIALS_JSON`; use the same profile
for `NOAH_PARAMETERS_JSON` in the environment file.

For the completed configuration, run the startup check:

```bash
.venv-validator/bin/python -m neurons.validator --env .env.validator --dry-run
```

This connects to the configured chain and checks startup conditions without
signing. A successful gate prints `THE GATE PASSED and this is a dry run, so
nothing was signed.` A failed later survey can still produce a nonzero exit
code. A successful dry run does not prove a complete scoring and publication
cycle. Leave the startup safeguards in place. The selected mainnet profile uses
direct publication through the configured proxy, with commit-reveal disabled,
matching network identities and the expected signer;
successful configuration does not authorize a real submission.

## Ark reference and first scoring period

The [address reference](reference/WALLET_ADDRESSES.md#on-chain-ark-reference)
must appear in subnet 82's profile for scored periods. This does not fund the
Ark. The adapter checks the record at the boundaries of each scoring day and
refuses a day whose ending block has no published reference. A key-free startup
check alone does not exercise or clear this historical requirement.

Before starting the publisher, reconcile the first scoring period with the
actual identity-publication block and retained history. Do not backdate the
identity, ignore a failed read, or erase contributions to make startup pass.
The operator must record the first supported scoring day and the treatment of
earlier records before publishing weights for those days.

## Chain connections and credentials

Both sources must serve the historical blocks needed by the scoring period.
They are compared for agreement; the second source is not automatic failover.
Two addresses pointing to the same node are not independent verification.
If one source is unavailable or disagrees, the scan refuses rather than using
one source alone or treating an unread earning as zero.

For a second endpoint whose address contains an API key, set
`NOAH_ENDPOINT_2_FILE` to a file outside the checkout and ordinary configuration
backups. Leave `NOAH_ENDPOINT_2` empty; setting both is refused. The file holds
only the endpoint address and must be readable by the validator's service user.
Do not put a keyed address into a shell command, issue, or support message.
A missing, empty, or unreadable file prevents startup.

This edition masks endpoint credentials in startup output, preflight reports,
errors, and operator tools. Regression tests use invented credentials to
exercise those paths. Keep the endpoint file readable only by the service
user and exclude it from ordinary backups. Do not turn on third-party debug
logging or paste credentials into support messages.

Budget provider requests for normal daily scanning, retries, and recovery
history. A fast connection can still exhaust a daily or monthly allowance.
Measure a full day on the installed release, not only a short read probe.
Request reduction reuses only redundant reads; source agreement and deliberate
fresh checks remain in place.

## History and recovery

Keep history and validator progress outside a replaceable checkout and back
both up. Restarting a validator must not discard its previous publications or
miners' contribution records.

A node's retained chain state and Noah's history database are different things.
The node can only answer historical reads it still holds. The database keeps
Noah's scanned earnings, deposits, and other facts. Ninety-day node retention
does not itself provide ninety days of past state on a freshly synchronized
node, nor does it replace the database or its backups.

Check an existing database without changing it:

```bash
.venv-validator/bin/python scripts/check_history_contiguity.py \
  --history-db /path/to/history.db --through-yesterday
```

Investigate recovery on a backed-up copy. Actual recovery requires the correct
network, Ark identity, Ark Validator, holding interval, and chain connections.
It can recover only data a source can still serve. Do not write to the
production history database before a reviewed recovery procedure establishes
the affected day range and a way to check the result.

A subnet number may have belonged to an earlier subnet. Recovery must use the
verified registration block of the current subnet as its history floor so
earlier activity is not attributed to it. Removing pre-floor rows alone can
leave no wallets to score; rebuild and verify affected days before publication.

For the release candidate's publication safeguards, read
[publication status unknown](runbooks/publication-status-unknown.md).
An unknown send outcome must be reconciled before another submission. Waiting
for an assumed transaction lifetime does not establish that nothing landed.

## Troubleshooting

| Problem | Check |
|---|---|
| No suitable Python or Node found | Versions on your terminal's path meet the requirements |
| Bittensor found in `.venv` | Keep computation and validator environments separate; do not install validator extras into `.venv` |
| TypeScript dependencies missing | Run `npm ci` inside `verification/noah-ts`, then repeat the check |
| Missing Ark identity, parameters, or holding interval | Supply verified configuration; do not copy test values into mainnet settings |
| History gap reported | Restore or recover the missing records before scoring |
| Chain refused | Confirm the network identity and the restrictions of this edition |

No production upgrade procedure or supported release series is established
yet. For local development, rerun installation and tests after updating the
checkout. Preserve any local data and configuration first.

### Automatic daily calculation evidence

The unattended validator now retains the exact input fixture and complete
Python/TypeScript-agreed result before publication. It uses the configured
submitted-record directory (`ticket-16/scores` in the normal runtime layout):

- `day-N.json`: existing score-output schema, with chain identity and the final
  `publishable_weights_u16` alongside the engine's `weightsU16`.
- `day-N.fixture.json`: the exact scoring input, including actual scoring dials.
- `day-N.calculation.json`: calculation manifest, proposed vector changes and
  links to immutable artifacts in `evidence/<sha256>.json`.
- `day-N.submitted.json` and the pending publication intent retain the immutable
  calculation reference. Direct confirmation still requires exact finalized
  chain evidence; a calculation artifact does not establish publication or payout.

The manifest retains the agreed bytes, input, day boundaries, source block
hashes, public chain identity and source fingerprints. Its context includes the
configured profile file captured at evidence-writing time; the fixture's dials
are the exact values the scorers used. A declared Git/deployment commit does not
prove installed equivalence: compare the recorded source hashes too.

Evidence-write failures stop a new automatic send. Conflicting canonical day
results are refused, rather than overwritten by an unattended run. Preserve the
entire evidence directory with the history database, intent and submitted
records when backing up or restoring. Missing or altered linked artifacts hold
direct recovery for reconciliation; deleting an intent is not a recovery step.
The best-effort diagnostic event log is separate from this backup evidence.

### Diagnostic event retention and health

With `NOAH_EVENT_LOG` enabled, the existing event writer now rotates the active
16 MiB JSONL file into three bounded archives (`.1`, `.2`, `.3`). A stable `.lock`
file coordinates writers across rotations. Rotation refuses symlinks and
non-regular destinations. An incomplete active-file tail is preserved and
reported as a write failure for operator repair; it is not silently discarded.
No event-file operation runs on the validator's payment thread.

`<event-log>.health.json` is a separate atomic diagnostic snapshot, refreshed
about every five seconds even when no new event succeeds. It contains the latest
writer's run/PID, written/dropped counts, queue depth, rotations, last successful
write, a bounded error category and available disk bytes as exact decimal text.
Check its timestamp: missing/stale health is unavailable, not healthy. When
multiple processes share a log, this file describes the latest writer, not an
aggregate of every process. `noah.runtime_events.sink_health()` exposes the local
process snapshot without disk I/O. Health-write failures and recovery produce
one sanitized stderr message per state transition. A recovered writer retains
its cumulative `losses_detected` flag. Abrupt process termination can still lose
queued diagnostic events.

`calculation.recorded` and `publication.state` distinguish calculated,
submitting, uncertain, rejected/refused, finalized and reconciled states. Direct
publication events include the persisted intent ID; automatic calculation
references carry genesis, artifact and source hashes across restart. These are
application observations; consult the referenced immutable calculation and
finalized readback records for evidence. Generic `stage.completed` still means
only that a function returned. Large monetary event values use
`amount_rao_decimal` when a JavaScript number could not represent them exactly.

Back up the complete calculation evidence directory, history database and
publication state together. Verify every linked artifact and reconcile any
possible post-backup transaction before resuming a restored publisher.
Rotating event logs remain diagnostics, separate from immutable calculation
artifacts, publication state and chain history.

### Daily stage observations

Each automatic loop tick now has a fresh `attempt_id` and uses the already
established chain/subnet/validator identity. A waiting tick cannot inherit a
previous tick's day, calculation hash or publication intent. `eligibility.state`
reports unreadable head/tempo, an unsettled period, the observed settled-height
candidate, and the resolved UTC day/block window. An observed tempo candidate
is not itself a completed-day or publication claim. `head_block` is the head
already used by that operation; it is not relabeled finalized.

`history.state` reports scanning, bounded progress once per 256 checked blocks,
completed input observation, reuse of an existing scanned-day marker, or refusal.
It adds no RPC calls and logs no source URLs. Generic stage events also carry
`elapsed_ms`; those timings measure local calls, not a production deadline.

`deposit.attribution` distinguishes a recorded attributed receipt from an
unqualified receipt, retaining public transaction/block/event, token, amount,
origin and Ark references. Unsafe refusal text stays out of diagnostics; the
history database retains the detailed evidence. `entry.qualification` separately
reports the existing per-seat entry gate and the already-computed entry credit
and entry day. Receipt attribution alone is not a scoring-credit decision.
`publication.prepared` follows durable calculation retention and links the
artifact, final seat count, changed-UID count, publication mode and proxy/direct
signing route. The linked manifest contains the actual vectors and changed UIDs.

`epoch.observed` and `accounting.observed` project only records the existing
reader has written after its block-identity checks. They distinguish attributed
versus withheld epoch amounts and observed versus unavailable locked-pay data.
Large `amount_rao`, `locked_arrival_rao` and `unlocked_rao` values use exact
`*_decimal` text. Unavailable amounts are omitted, never fabricated as zero.
Historical observation events use `scan_day` and omit calculation/intent links:
this scan may read earlier epochs, and it does not establish which publication
was applicable, reconcile expected payouts, or classify returned collateral
principal as earnings. These events alone do not prove settlement.

## Optional epoch maintenance

[Daily scoring with epoch maintenance](EPOCH_MAINTENANCE.md) describes the
opt-in collection and republication settings. Both default to false. They
do not change the daily scoring formula or UTC cutoff. Review and rehearse
them before enabling; each extra publication incurs a transaction fee.
