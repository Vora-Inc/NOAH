# Scoring

Noah scores contributions against spendable mining earnings. The mainnet target
is 50%. A miner who consistently meets that target receives approximately a
1.0 contribution multiplier, subject to the configured cap and integer
rounding. The final reward weight also depends on scoring credit, streaks,
and eligibility.

This page describes the included implementation. Test fixtures carry their own
parameters; they are not the mainnet configuration. See the
[mainnet profile](parameters/profiles/mainnet-production-v1.json).

## The three parts

**Contribution compliance** measures qualifying NOAH contributed against
eligible earnings. Extra contributions can cover future obligations. Later
contributions can repay earlier shortfalls.

**Scoring credit** accumulates from contributions and ages over time. Older
credit has less influence. This aging does not reduce the number of tokens
still owed or make unused advance coverage expire.

**A contribution streak** records whether each day's requirement was met.
An advance contribution can cover a future day. Catching up later does not
rewrite an earlier missed day or rebuild its streak.

Keeping these separate matters. Two miners can have contributed the same total
amount and still have different scores because their histories differ.

## Days and spendable earnings

The engine processes UTC calendar days in order. On day `d`, it compares
contributions made that day with spendable pay from day `d-1`. Deposits retain
their actual transaction date.

Spendable pay is newly spendable earnings plus previously locked earnings that
unlock that day. Earnings that are still locked do not enter the obligation
until they unlock. **Returned registration collateral is not earnings.** It
must not be added to the contribution requirement merely because it becomes
spendable. The chain reader distinguishes those records. Verify the daily
inputs against finalized chain records.

There is no noon cutoff that makes a contribution stop counting. A later
contribution can still repay a shortfall. Daily scoring and streak
classification mean timing can affect a score, even though the obligation
and advance coverage do not expire.

A zero-pay day creates no new contribution obligation. Streak classification
is a separate check: the included implementation treats a zero-pay day as
neutral only when it is marked `noFault`; otherwise it is a miss. An outage
must therefore be represented correctly rather than silently assumed excused.

## New miners

A qualifying 10 NOAH entry contribution adds starting credit. It is excluded
from the ongoing contribution index and does not itself count as a daily
streak contribution.

The contribution multiplier is fixed at **1.0** from the miner's first activity
through the day it first receives spendable mining earnings. Ordinary
PCI-based scoring begins the next day. This is a starting multiplier, not an
assumption that the contribution index is 1.0. Eligibility checks still apply.

## Shortfalls and advance coverage

At a 50% target, 100 NOAH of eligible earnings creates a 50 NOAH obligation.
The examples below assume no other obligations or contributions and exclude
the entry deposit.

| Contributions against that obligation | Coverage result |
|---|---|
| 50 NOAH | Obligation covered |
| 30 NOAH | 20 NOAH remains owed |
| 70 NOAH | Obligation covered; 20 NOAH available for later earnings |
| 30 NOAH, then 20 NOAH later | The shortfall is cleared when the later payment arrives |

The debt and unused coverage are token balances: neither decays. The engine
separately tracks an age-adjusted value for old debt so a late payment does not
receive fresh compliance credit against an old, already-aged earnings record.
A catch-up payment reduces the actual debt by its full qualifying amount.

The code applies a day's contribution to that day's matched requirement,
then uses any surplus to repay outstanding debt before banking it for later
requirements. It does not reuse the same deposit for both debt and advance
coverage. See [reserve.py](../noah/scoring/reserve.py) and
[engine.py](../noah/scoring/engine.py) for the calculation order.

## From contributions to weights

The contribution index, `PCI`, is the age-adjusted NOAH coverage divided by
age-adjusted spendable pay. It is **not** deposits divided by the target amount.
At a 50% target, an on-target miner's index is approximately 0.5.

For ordinary scoring, the multiplier is:

```text
multiplier = min((PCI / target) ^ harshness, cap)
```

For illustration, with a target of 0.5, harshness of 2, and cap of 1.0, a PCI
of 0.25 gives a multiplier of 0.25. A PCI of 0.5 gives 1.0. These values
match the checked-in mainnet profile; the example omits the miner's other
score inputs.

The engine then combines accumulated NOAH credit, permitted other-token
matching credit, the streak factor, and the holding gate. Holding less than
the configured minimum closes the gate and gives zero score; holding more
than the minimum does not increase that factor. The checked-in mainnet profile
sets the extra holding minimum to zero.

```text
base = NOAH credit + other-token matching credit
score = floor(floor(base × streak / 10^9) × slot / 10^9) × gate
pay weight = floor(integer_sqrt(score) × multiplier / 10^9)
```

The slot factor is currently 1.0. Other-token credit is limited so it cannot
exceed NOAH credit and does not satisfy NOAH contribution compliance. Eligible
pay weights are converted into the chain's integer weight vector. These are
relative weights, not a promise of a particular daily payout.

All normative arithmetic uses integers with floor division in a fixed order.
Fractions use a scale of `10^9`: 1.0 is `1000000000`. Small rounding differences
from a decimal calculation are expected.

## Check a score

From the repository root, after `./scripts/install.sh`:

```bash
.venv/bin/python -m noah.scoring.audit_published \
  fixtures/shared/steady-at-target.json \
  fixtures/shared/out-python/steady-at-target.score.json
```

Expected output and exit status:

```text
AGREES: the published figures follow exactly from the published inputs.
```

Exit code `0` means the recomputed result agrees. For a published day, replace
the two paths with its fixture and score files from the daily bundle. See
[score verification](VERIFYING_SCORES.md).

The command recalculates the score using the Python engine. The separate
TypeScript implementation can also check the same fixtures:

```bash
./scripts/install.sh
./scripts/twin-proof.sh
```

The twin check compares canonical output files byte for byte.
Agreement on fixtures tests the arithmetic; it does not independently prove
the chain inputs or every possible scoring scenario.

## Input reference

Inputs use the `noah-fixture/6` schema. The
[parser](../noah/scoring/fixture_io.py) defines the complete contract.

| Field | Meaning |
|---|---|
| `targetFp` | Contribution target; `500000000` means 50% |
| `harshness`, `capFp` | Multiplier curve and ceiling |
| `halfLifeDays` | Aging of scoring credit and index values |
| `streakCeilingFp`, `streakRampDays` | Streak growth and maximum |
| `ongoingHoldRao` | Minimum holding needed for eligibility |
| `entryDepositRao` | This miner's entry credit, separate from daily deposits |
| `paidRao` | Pay that arrived spendable on the recorded day |
| `paidLockedArrivalRao` | Pay that arrived locked; excluded until it unlocks |
| `payUnlockedRao` | Previously locked pay becoming spendable that day |
| `noahDepositRao` | Qualifying ongoing NOAH contribution recorded that day |
| `foreignDepositNoahEquivRao` | Other-token contribution value in NOAH units |
| `noFault` | Explicit exemption used in daily streak classification |

The validator's record must cover the required history. A missing day is not
zero activity. See [history and recovery](VALIDATOR_QUICKSTART.md#history-and-recovery).
