> Version scope: this document retains predecessor rules and receipt/history details. For candidate `points-204040-v5`, [ordinary scoring](ORDINARY_SCORING.md) governs onboarding, contribution, bonuses and publication. Historical starter descriptions below do not activate a pool in V5.

# Starter entry policy: shared 0.25% pool

Mainnet activated this policy on November 3, 2026. New miners do not need
an upfront NOAH purchase or Ark deposit. Registration fees, eligibility checks
and the ongoing contribution rules still apply.

## Mainnet activation and reproduction

The live profile is `mainnet-production-starter-25bps-v1`. The historical cutoff
is UTC day **1323** (November 2, 2026), block **9,421,694**; the first new-policy
day is **1324**. The frozen snapshot contains **67 records: 64 with preserved
historical entry credit and three with zero entry credit**. Preserved credit is
not a new payment requirement.

See [reproduce the Mainnet entry snapshot](MAINNET_STARTER_SNAPSHOT.md) for the
fixed block hash, content commitments and a read-only reconstruction procedure.
Do not create a new cutoff using today's date. The validator and independent
guard must share the verified original snapshot; see
[historical guard startup](LEGACY_VALIDATOR_QUICKSTART.md#start-the-guard-and-validator).

## For miners

Under `noah-starter-5noah-shared-25bps-v1`, register a hotkey normally. No initial
ten-NOAH purchase or ARK deposit is required for temporary starter eligibility.
Registration and transaction fees still apply. There is no axon requirement.
Wait for the next completed UTC-day roster and a finalized epoch allocation.
Check the published evidence for your coldkey/hotkey, status, remaining allowance
and weight. An empty pool or unavailable verified history can delay allocation.

The allowance is approximately **five spendable NOAH over the lifetime of the
coldkey and of the hotkey**, whichever limit is reached first. Additional hotkeys
under the same coldkey share its allowance and its pool share. Changing only a
hotkey, only a coldkey, re-registering, or obtaining another UID cannot reset
already counted earnings. Distinct coldkeys are not proof of distinct people.

All starters together receive at most **0.25% of this validator's submitted mining
weight**. Eligible coldkeys share capacity equally, subject to their remaining
allowances and target pace; hotkeys divide their coldkey's share. Rounding is
deterministic. The individual target is at most five NOAH per day before the
shared cap and pending-reward reservation. It is not a promised daily payment.
Unused capacity stays with ordinary miners in their existing score proportions.
There must be positive ordinary weight: no remainder is assigned to the owner
and starters are never deliberately allocated the whole mining pool.

Starter earnings count toward the same **50% contribution target** as other
eligible earnings. They are not an unconditional gift. Early contributions are
retained, but ordinary weight and starter weight are not stacked. After the
allowance is exhausted, ordinary scoring determines rewards. Without contribution
credit there is no ordinary score. A daily scoring boundary may delay that
transition. Follow the existing attributed contribution process; do not send an
unidentified transfer or expect a deposit to bypass receipt validation.

## What does not change

The ordinary formula, 50% target, 2x contribution cap, decay, consistency, debt,
prepaid and foreign-token matching rules remain unchanged. Existing qualifying
entry receipts retain their original ten-NOAH credit and original receipt dates.
Those tokens are excluded from ongoing contributions exactly once. The profile
still records ten NOAH for historical recognition; setting that field to zero
is not how this policy is activated.

Established miners retain their relative ordinary scores. Their combined mining
share is reduced by the starter allocation, capped in our submitted vector as
above. Other validators, Yuma consensus and changing emissions affect actual
payouts, so this is not a chain-enforced token or consensus cap.

## Verifying the evidence

The daily Python/TypeScript score is retained unchanged. Each starter publication
adds a content-addressed record linking that daily calculation, current finalized
block, actual epoch boundary, lifetime earnings including departed identities,
pending-reward estimate, policy ID and exact submitted vector. A separate
JavaScript integer implementation checks the allocation before publication.
Receipts verify finalized inclusion and the runtime's rescaled weights.

Input schema `/4` uses actual epoch **block numbers**, not a fixed wall-clock
schedule. `weightEffectiveEpoch` and `pendingThroughEpoch` in that schema are
block boundaries. The older `/3` synthetic-clock input is for offline models and
is refused by the publisher. Every publication expires locally at its specified
`nextEpochBlock`; that expiration is not a chain-level expiry.

## Outage behavior and limits

An independent guard process shares the short publication lock and durable intent
with the normal validator. If a starter allocation expires, it requests and
verifies an ordinary-only replacement. It does not need the daily calculation
lock. New starter sends require a successful guard heartbeat no older than
120 seconds. This bounded allowance covers serialized finalized-inclusion waits;
lock contention never renews the heartbeat. A stuck publisher or dead guard still
fails health and blocks new starter sends after that deadline. Unknown
transaction outcomes are reconciled before another send; they are never discarded.

If both processes or the host fail, or if the chain refuses replacement, old
weights can continue paying. Other Yuma validators may also retain different
weights. Five NOAH is therefore an allowance target, **not a guaranteed exact
maximum payout**. Independent monitoring must alert on a stale heartbeat,
unresolved intent and expired starter weights. Recovery is an operational
requirement, not something a local arithmetic test proves away.

## Manual day-close limitation

`day_close_pipeline.py` does not implement starter-entry policy. When
`NOAH_STARTER_ENTRY_SNAPSHOT` is configured, it returns exit code 2 before connecting, opening
a wallet, reading chain history, or creating/changing evidence. This applies to
dry-run, `--fixture`, `--publish`, and `--publish-direct`. Direct Python callers
also refuse when the environment enables starter entry or the adapter already has
a starter policy attached.

Use the normal configured validator workflow for starter-enabled scoring and
publication. Do not unset the snapshot setting to work around this refusal. This
restriction does not change ordinary-mode scoring or the starter allocation rules;
it prevents an unsupported manual command from producing misleading evidence or
publishing a vector without applying those rules.
