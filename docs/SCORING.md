# Scoring

Noah scores contributions against spendable mining earnings. The mainnet target
is 50%. A miner who consistently meets that target receives approximately a
1.0 contribution multiplier, subject to the configured cap and integer
rounding. Qualifying surplus can increase this multiplier up to **2.0** under
the checked-in mainnet profile. The final reward weight also depends on scoring
credit, streaks, and eligibility.

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

The baseline contribution index, `PCI`, is age-adjusted NOAH coverage divided
by age-adjusted spendable pay. It is **not** deposits divided by the target
amount. At a 50% target, an on-target miner's index is approximately 0.5.
The surplus rule below can raise this baseline index.

For ordinary scoring, the multiplier is:

```text
multiplier = min((PCI / target) ^ harshness, cap)
```

With the checked-in mainnet target of 0.5, harshness of 2, and cap of 2.0, a
PCI of 0.25 gives a multiplier of 0.25; a PCI of 0.5 gives 1.0. A qualifying
surplus PCI of 0.6 gives 1.44. These examples omit the miner's other score
inputs and the starting-period override described above.

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

## Surplus contribution bonus

The engine considers a surplus bonus when all of these hold:

- The configured multiplier cap is greater than 1.0.
- Outstanding contribution debt is zero.
- Unused advance coverage is greater than zero.
- Cumulative prior-day spendable earnings are greater than zero.

It sums qualifying regular NOAH deposits and prior-day spendable pay over the
miner's complete recorded history, starting at its first activity day:

```text
surplus ratio = cumulative regular NOAH deposits / cumulative prior-day spendable pay
PCI = max(baseline age-adjusted PCI, surplus ratio)  # only if ratio > target
multiplier = min((PCI / target) ^ harshness, cap)
```

The entry contribution, other-token matching credit and draws from existing
coverage are **not** new regular deposits in this ratio. Locked pay enters the
denominator only after it becomes spendable and reaches the next day's
comparison. The evaluated day's newly spendable earnings enter that comparison
the following day. Old debt must be fully repaid first; paying only enough to
clear a shortfall does not create a surplus bonus. The starting-period 1.0
override and eligibility gates still apply.

At a 50% target and harshness 2, a qualifying cumulative contribution ratio of
about **70.71%** reaches the 2.0 cap (`0.5 × sqrt(2)`). This is a contribution
multiplier, not a promise of twice the tokens or twice another miner's reward.
The implementation uses fixed-point integer arithmetic and saturation before
the existing multiplier calculation; boundary values are subject to rounding.

**The surplus ratio does not age.** Scoring credit and the baseline compliance
index still age; cumulative deposits, cumulative prior-day pay, actual debt and
unused coverage do not. New earnings dilute a fixed surplus over time. For the
same absolute extra contribution, a shorter earnings history receives a larger
bonus:

| Complete prior-pay history | Earnings/day | Regular contribution/day | Extra on evaluation day | Multiplier |
|---|---|---|---|---|
| 5 days | 100 NOAH | 50 NOAH | 100 NOAH | 1.96× |
| 365 days | 100 NOAH | 50 NOAH | 100 NOAH | approximately 1.01099× |

These examples assume no other debt or contributions, ignore entry credit,
and are beyond the starting period. Their ratios are `350/500` and
`18,350/36,500`. Histories with the same cumulative contribution percentage
have the same surplus ratio; the difference above comes from comparing a
fixed extra amount against different cumulative earnings. Aging this ratio
would be a separate scoring-rule change, not a documentation correction.

## Scoring-rule identity

The surplus-bonus rule set is identified by
`noah-scoring/surplus-bonus-v1`, with publication `version_key`
`208370384225125`. [version.py](../noah/core/version.py) derives this number
from the declared identifier. The previous identifier `noah-fixture/4`
produced `201647293659568`; the identity update distinguishes the rule set
without changing the already implemented surplus arithmetic.

This publication identifier is separate from the input/output schemas and
from the parameter profile. The input schema remains `noah-fixture/6` and the
output schema remains `noah-score-output/6`. Reproducing a score requires its
inputs, parameters and source revision as well as the declared rule set; the
identifier is not an automatic fingerprint of every source file and is not
currently embedded in every standalone score JSON.

The weight-submission call carries the numeric version alongside its UID/weight
vector. Updating the identifier changes that version field, not the calculated
vector. The subnet's minimum accepted version is a separate chain policy.
Hash-derived identifiers are not numerically ordered by release date: check
compatibility with the existing minimum before a validator update. This
release does not itself change that chain setting or authorize publication.

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
| `halfLifeDays` | Aging of scoring credit and baseline index; not the cumulative surplus ratio |
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
