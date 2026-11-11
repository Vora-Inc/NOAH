# Ordinary scoring: 20 / 40 / 40

> The current release candidate is V2.0, internally identified as `points-204040-v5` (policy/5). Historical policy identifiers remain unchanged for verification.

**Review candidate, not an activation announcement.** This describes mode
`points-204040-v5`. Publishing source does not activate this mode on a validator.
Check the scoring mode in the retained calculation before interpreting live rewards.

## Joining

Register a miner hotkey normally. There is no upfront NOAH purchase, ten-NOAH
entry deposit, five-NOAH allowance or reserved starter pool in this mode.
An eligible newcomer starts with **0.2 points**: 20 base points at 1% maturity.
It enters the first completed-day calculation with verified identity and history.
Registration alone does not cause immediate payment: publication and the chain's
next applicable epoch must follow. Registration charges and transaction fees remain.

If a miner actually earns 10 NOAH on Monday UTC, 5 NOAH-equivalent becomes due
on Tuesday UTC, assessed at Tuesday's close. No earnings means no new obligation.
Existing debt is not forgiven on a day with no earnings.

## The score

For a miner whose obligation is covered:

    score = (20 base + contribution bonus + consistency bonus) × maturity

| Component | Rule |
|---|---|
| Base | 20 points |
| Contribution | 0 to 40 points, based on cumulative credited contributions divided by cumulative earnings already due |
| Consistency | 0 to 40 points for consecutive earning days with a real payment meeting that day's 50% obligation |
| Maturity | 1% initially, growing with a squared curve to 100% at 90 qualifying earning days |

The final score is **not squared or cubed**. A fully mature miner can earn at most
100 points. Weights are proportional to final points, rounded to the chain's
integer weight units. Other validators, consensus and the size of the emission
pool affect actual payments; points are not a promise of a fixed NOAH amount.
Configured owner-seat exclusions remain enforced.

### Contribution bonus

At 50% cumulative contribution the bonus is zero. At 60% it is 16 points.
At 75% or more it is 40 points. Depositing above 75% buys no additional
contribution bonus, but surplus still covers future obligations.

    bonus = clamp(160 × cumulative credited contributions / due earnings − 80, 0, 40)

The ratio uses credited ongoing contributions, including qualifying foreign
receipts. Historical entry tokens already excluded by migration are not counted
again. The migration balance adjustment is not a new deposit or bonus credit.
There is no bonus when the denominator is zero.

### Consistency and maturity are separate

    consistency = 40 × (min(consecutive qualifying payments, 30) / 30)²
    maturity = 0.01 + 0.99 × (min(max(qualifying days − 1, 0), 89) / 89)²

A 15-day payment streak earns 10 consistency points; 30 days earns 40.
Day zero and the first qualifying earning day have 1% maturity; qualifying day
90 reaches 100%. These are earning/compliance days, not simply calendar age.

Prepaid coverage can advance maturity on a positive-earning day. It cannot
pretend a payment occurred. An earning day without a real payment of at least
the new daily obligation resets the consistency streak, even if prepaid credit
covers the obligation. A zero-new-earnings day adds no streak or maturity day
and preserves the streak if there is no debt. Previously earned maturity is
retained after a miss. Clearing the first genuine debt can establish the first
qualifying day even when no new earnings are due; it cannot repeatedly mature
a miner on empty days.

## Debt, prepayment and catch-up examples

- Earn 100, deposit 50: the obligation is covered. Normal scoring applies;
  the payment can count toward the daily streak.
- Earn 100, deposit 100: 50 covers the obligation and 50 remains prepaid.
  If the next day's obligation is 50 and nothing is deposited, there is no
  debt penalty, but that day's missing actual payment resets the streak.
- Have no prepaid credit and leave 50 due: the first uncovered day receives
  **half the last compliant score**, with no new bonuses. A second consecutive
  uncovered day receives **zero**, even if no additional earnings arrived.
- Pay only part of the debt: the uncovered-day count does not reset.
- Pay all outstanding debt: normal scoring resumes with earned maturity and
  current contribution credit. The old streak is not restored. The catch-up
  day starts a new streak only if new earnings are due and the actual payment
  also meets that day's obligation.

A daily calculation and successful chain publication must occur for these
changes to affect rewards. A score of zero in a preview is not immediate proof
of zero chain emissions. Without a reserved pool, a new miner's percentage also
depends on other scores. If all other miners score zero, a small positive score
can receive a large share; the 0.2 starting score is not an absolute payout cap.

## Which tokens count

**NOAH is not mandatory.** Attributed NOAH, eligible foreign subnet alpha,
root TAO, or a mixture can meet the obligation. Tokens must actually reach the
Ark with the required Ark Validator delegation and miner attribution.
Keeping stake in your own wallet does not count.

Native NOAH counts by its actual received amount. Foreign and root receipts use
the existing authenticated parent-block EMA conversion and spot-price safeguard:

    credited NOAH-equivalent = received amount × min(foreign EMA / NOAH EMA,
                                                     foreign spot / NOAH spot)

The reference is the finalized block immediately before the deposit. Root TAO
has a price numerator of one TAO. The selected chain EMA source is pinned by
the preserved pricing policy. There is no separate root-token bonus, no native
matching requirement and no change to the predecessor EMA policy in this update.
Unsupported or unverifiable assets are not arbitrary credits: their evidence
and status must remain visible. Missing/contradictory required evidence prevents
a new verified daily calculation; it does not silently erase debt or invent prices.

Historical accepted credits and balances retain their migration treatment.
The historical entry snapshot remains necessary to prevent lost or double-counted
credit. Old source files and historical examples do not activate a starter pool.

## Publication and outages

The validator calculates daily scores from finalized records, collects history
throughout the day and refreshes the verified daily weights each epoch. Refreshing
weights is not recalculating scores. Verified weights may be refreshed within the
approved 24-hour daily-publication outage limit, subject to identity and evidence
checks. No new refresh is authorized after expiry. Recovery can still verify an
already submitted transaction included after midnight; that does not extend the
send deadline. Unknown transactions remain a refusal, not an assumed success.

No new research-work bonus is implemented here. That would require a separate
specified, reviewed rule. See [validator setup](VALIDATOR_QUICKSTART.md) and
[verification](VERIFYING_SCORES.md).
