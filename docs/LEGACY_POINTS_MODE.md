> Historical documentation. These are predecessor rules, not instructions for points-204040-v5. See [ordinary scoring](ORDINARY_SCORING.md).

# Optional points scoring mode

The default remains `legacy`. Historical `points-402040-v2` remains readable;
the prospective amendment is `points-402040-v3`. Selecting its profile alone
does not activate it. The validator requires the mode, profile,
immutable points policy, starter snapshot and direct epoch maintenance together.

## Scoring

The mode uses a single NOAH-equivalent balance. A day's obligation is half the
previous day's eligible earnings. Deposits first settle obligations and debt;
positive balance remains prepaid credit. Root TAO and other subnet tokens can
contribute without a native-token deposit. Foreign receipts use the previous
completed UTC day's authenticated average price relative to the configured native
subnet. The amount actually received is credited; there is no deposit-size cap,
liquidity haircut or price-movement veto.

Ordinary points combine 40 baseline points, up to 20 contribution points and up
to 40 daily consistency points, multiplied by earned maturity. Maturity begins
at 10% on the first qualifying day and reaches 100% at 30 qualifying days.
Prepaid credit can cover obligations and advance maturity; consistency requires
a sufficient real deposit that day. A zero-earnings day creates no new obligation
or consistency day. Old debt remains owed.

The first uncovered day earns half the last compliant score; the second earns
zero. Full catch-up restores normal scoring without repairing missed days.
A newcomer can earn its first maturity day by clearing its first genuine debt
even when starter earnings have stopped. This bridge cannot be repeated to build
maturity without new earnings.

## Prospective reference amendment

The v3 amendment preserves the exact original v2 policy bytes, cutoff, frozen
valuations, financial lineage balances, entry identities and consumed receipt IDs.
It changes only prospective reference eligibility from the first day after that
same cutoff. Previously valid credits use the same previous-UTC-day average and
the same integer rounding.

For each foreign token, both finalized readers must authenticate stored subnet
membership and the same `(NetworkRegisteredAt, RegisteredSubnetCounter)` marker
at the receipt, its parent, and every positive-duration reference interval.
Known runtime code and storage semantics are checked. A `(0, 0)` marker can be
legitimate; this is observed continuity, not a claim about genesis origin. Root
TAO uses its fixed numerator and the same authenticated native denominator.
Direct native-token repayments are unchanged.

Proven reference absence, a different token incarnation, or a successfully read
zero numerator or native denominator makes that receipt unsupported: its exact
event and reason remain visible with zero credit, and other receipts can settle.
This does not forgive existing debt or create compliance credit. Unavailable or
contradictory evidence, unknown runtime semantics, and ambiguous receipt-block
generation remain pending and refuse the daily calculation. A positive runtime
API default alone is not membership evidence. Cached prices cannot bypass the
lifecycle proof. Shared immutable window proofs avoid repeating a full walk for
each receipt; individual receipt proofs retain their actual block and parent.

The unsigned document builder preserves the predecessor literally:

```sh
python -m noah.points.amendment \
  --predecessor /path/to/original-points-policy.json \
  --predecessor-sha256 ORIGINAL_FILE_SHA256 \
  --output /path/to/points-policy-amendment.json
```

This creates a document only. A managed pre-first-publication transition must
reconcile both readers' current legacy receipt/vector, reject prior prospective
points sends and pending intents, preserve the original configured file and
archive, and explicitly install the successor under the release transaction.
Normal v3 startup refuses missing or mismatching durable state; deleting policy
pins or selecting an environment variable does not perform a migration. The
read-only `--amendment-transition` preflight checks those prerequisites without
installing state. A previously published v2 ledger needs a separately designed
migration; this path does not reinterpret it.

V3 calculations use `noah-points-fixture/2` and `noah-points-output/2`, with
`receiptDispositions` present even when empty. Unsupported receipts remain distinct
from pending receipts. The retained proof authenticates reference eligibility;
normal dual-reader event ingestion authenticates transfer identity and amount.

## Cutover and configuration

Use a finalized, retained legacy calculation to prepare the immutable cutoff
policy. The policy authenticates its chain, cutoff block, identities, historical
receipts and balances. Cutover preserves each migrated identity's legacy debt or
prepaid balance exactly. Other points components are derived from verified
history. The balance bridge is not a deposit and creates no extra contribution
credit. New daily calculations begin after the cutoff.

Preparation requires a legacy calculation collected with
`ChainAdapter.capture_points_lineages = True`. Its retained context must include
the complete `points_lineage_capture`, including historically attributed
coldkey/hotkey pairs that have no active UID at the cutoff and the original frozen
entry identities. Ordinary legacy calculations do not collect this extra context.
The resulting `noah-points-policy/2` binds the full capture and original starter
snapshot. Earlier policy documents without this complete lineage evidence are
refused.

Native stake-and-hotkey transfer anchors additionally retain an exact receipt
proof. Both readers must reproduce its event identity, actual received amount,
asset, sender, origin hotkey and Ark destination through the existing deposit
decoder. Ownership of a transferred stake position is not a claim of global
hotkey ownership. Other historical anchors retain their ownership checks;
missing or contradictory attribution refuses preparation or startup. Existing
policies with verified owner-based anchors remain supported unchanged.

A returning identity keeps its own historical debt, prepaid balance and earned
maturity, even when its UID changes. Another hotkey sharing its coldkey cannot
inherit them. The original entry receipt stays consumed. A frozen entry identity
with no financial history starts fresh when its first real activity occurs.
The cutoff block and its finalized successor must straddle the UTC day boundary.

The preparation command is unsigned and does not publish:

```sh
python -m noah.points.prepare \
  --evidence-dir /path/to/scores \
  --reference /path/to/calculation-reference.json \
  --pricing /path/to/pricing-policy.json \
  --receipt-ids /path/to/consumed-receipt-ids.json \
  --out /path/to/points-policy.json
```

The pricing policy is:

```json
{
  "method": "previous-completed-utc-day-average",
  "assets": "all-subnet-tokens-and-root",
  "credit": "actual-received"
}
```

Keep the existing chain, signer, history and runtime configuration. The additional
settings are:

```dotenv
NOAH_SCORING_MODE=points-402040-v3
NOAH_POINTS_POLICY=/path/to/points-policy-amendment.json
NOAH_PARAMETERS_JSON=/path/to/docs/parameters/profiles/mainnet-production-points-402040-v1.json
NOAH_STARTER_ENTRY_SNAPSHOT=/path/to/existing-starter-snapshot.json
NOAH_PUBLISH_MODE=direct
NOAH_EPOCH_COLLECTION=true
NOAH_EPOCH_REPUBLICATION=true
```

Reconcile outstanding sends and remove the old starter overlay under its old
configuration before selecting the new mode. Preserve durable history and
receipts. The preflight checks the selected policy against both configured
readers without loading a wallet, installing policy or publishing:

```sh
python -m noah.points.preflight --env /path/to/validator.env
```

The points mode binds retained calculations and guard health to its policy.
A restart with that same policy is supported. A different policy or a reversal
of accounting mode requires a reviewed migration; replacing files or deleting
history is not a migration.

## Missing pricing evidence

Both readers need every price interval in the previous completed UTC day and
the blocks establishing its boundaries. Boundary searches step backward from
the finalized receipt, rather than reading genesis state. Their expanding
search steps can probe older blocks than the exact day needs, so retaining only
that minimum window does not guarantee availability. An unreadable probe or
disagreeing timestamp refuses collection; no timestamp or price is estimated.

Durable price series reduce repeated price queries; they do not replace the
historical state required from both readers. Replay still verifies receipt
timestamps and boundaries, and startup verifies historical migration and starter
identities. Readers with rolling pruning will eventually refuse these checks
once required state is removed. Keep the required historical archive available
on both readers; a cached price series alone cannot provide recovery after that
state is pruned.

Unavailable reference data leaves the receipt pending and the daily calculation
retrying. Previously verified ordinary weights may refresh only within the
bounded 24-hour outage window. Starter allowance is not repeated. A disagreement
or identity failure must not authorize an outage refresh. Publication still
requires verified source identities, policy and transaction reconciliation.
