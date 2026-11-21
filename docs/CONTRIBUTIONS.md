> The current release candidate is V2.0, internally identified as `points-204040-v5` (policy/5). Historical policy identifiers remain unchanged for verification.

> Candidate scope: `points-204040-v5`. [Older contribution rules](LEGACY_CONTRIBUTIONS.md) remain available for historical replay.

# Contribution rules

> **Current Mainnet entry policy:** Starter entry is active from UTC day 1324 (November 3, 2026), after cutoff block **9,421,694**. New miners do not need an upfront NOAH purchase or Ark deposit. Registration fees, eligibility checks and ongoing contribution rules still apply. See [starter entry](STARTER_ENTRY.md).

A Noah contribution transfers ownership of staked tokens to the Ark. Staking
tokens to the Ark Validator while retaining them in your own wallet does not
count. The validator checks both the transfer and the stake received.

## Address map

| Address | Role |
|---|---|
| Your coldkey | Owns the funds you contribute |
| Your miner hotkey | Identifies the miner that should receive scoring credit |
| Ark coldkey | Receives ownership of the contributed stake |
| Ark Validator hotkey | Required delegation for the stake arriving in the Ark |

### Mainnet addresses: Subnet 82

| Purpose | Public address |
|---|---|
| Ark receiving coldkey | `5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3` |
| Required Ark Validator hotkey | `5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV` |

These addresses are also listed in the
[mainnet address reference](reference/WALLET_ADDRESSES.md). Check them against
current chain records and the official Noah website before sending.

The recipient is the Ark receiving coldkey, **not** the Ark Validator's
controlling multisig. The delegation destination is the Ark Validator hotkey,
**not** the Noah scoring validator. These addresses apply only to Finney
mainnet; never copy them into a configuration for another chain.

The [on-chain address reference](reference/WALLET_ADDRESSES.md#on-chain-ark-reference)
is a field in Noah's subnet profile. It identifies the existing Ark recipient;
it does not turn the Ark into a subnet or give the owner control over its funds.

## What the validator checks

A qualifying contribution must arrive at the official Ark coldkey,
delegated to the designated Ark Validator, and be attributable to an eligible
miner. The reader uses chain events and, where needed, the transaction's
contents to establish those facts.

The destination delegation is the requirement. The source stake does not
necessarily have to be delegated to the Ark Validator already: a transfer
that changes both ownership and staking hotkey can arrive with the required
delegation.

A transfer to the Ark under the wrong validator is still a real receipt, but
it earns no qualifying mining credit. Every contribution must identify its
miner, even when a wallet has only one miner.
An unnamed deposit receives no mining credit.

## Ordinary onboarding and ongoing contributions

In review candidate `points-204040-v5`, a new miner starts with 0.2 points and no
entry deposit or starter pool. Its actual earnings create the 50% contribution
obligation the next UTC day. NOAH, qualifying foreign alpha, root TAO or mixtures
can satisfy it. Read [ordinary scoring](ORDINARY_SCORING.md) for exact conversion,
bonuses, prepayment, debt and catch-up rules. This is not an activation claim.

Historical entry amounts, consumed receipts and balances retain their frozen
migration treatment. They are not new deposits and cannot be counted twice.
The historical snapshot is an accounting record, not an active allocation pool.

## Shared wallets

When several miner hotkeys share a coldkey, their credit must remain separate.
A receipt identifying one miner does not credit its neighbors. An unidentified
receipt is not evidence that every miner in the wallet contributed.

The current parser supports two attribution mechanisms:

| Mechanism | How the miner is identified |
|---|---|
| `transfer_stake_and_hotkey` | The transfer event's `origin_hotkey`, with ownership checked |
| Top-level `utility.batch_all` containing `transfer_stake` and `system.remark_with_event` | A validated 32-byte miner hotkey in the remark, with ownership checked |

The current reader marks newly read unnamed deposits as uncredited, including
for wallets with only one miner. A legacy database row without attribution
metadata can still use a compatibility path that matches its stored hotkey.
Operators must review those old records when upgrading; changing the reader
does not automatically correct historical rows. Use a transaction that
identifies your miner, whether the wallet has one seat or several.

These describe reader behavior, not complete wallet instructions. The first
mechanism can preserve attribution inside a wrapper because the identity is
in the event itself. The second depends on parsing the top-level batch and
does not establish general support for nested proxy or multisig transactions.
A guided, rehearsed participant flow is still required before recommending
those paths.

## Advance contributions and catch-up

Unused coverage is held for future earnings without a cap or expiry. If you
already have a shortfall, surplus contributions repay it before becoming
advance coverage. No contribution can be spent twice in this accounting.

For example, 100 NOAH of eligible earnings creates a 50 NOAH obligation. If
you contribute 70 NOAH against it, 20 NOAH remains available for later
obligations. If you first contribute 30 and later contribute 20, the later
payment clears the 20 NOAH shortfall. These examples exclude any preserved
legacy entry credit and assume no other earnings or deposits in the interval.

That is the coverage calculation. Your score also depends on maturity, cumulative contribution and
streak history. Clearing a debt does not turn every previous missed day into
an on-time contribution. The [scoring guide](SCORING.md) separates those parts.

## Other subnet tokens and root TAO

The candidate accepts attributed eligible foreign alpha and root TAO without
requiring a native NOAH deposit or matching native credit. Native NOAH counts
by amount. Foreign/root value is converted once to NOAH-equivalent using the
verified block immediately before its receipt, applying the lower of the
foreign/NOAH EMA ratio and spot-price ratio. Root TAO has a one-TAO numerator.
See [the complete valuation rule](ORDINARY_SCORING.md#which-tokens-count).
Historical accepted credits are preserved rather than repriced.

## Checking a receipt

Check the actual stake amount received, not just a displayed TAO-equivalent
transfer amount. The reader joins the transfer and stake events from the
transaction to establish ownership and the received alpha amount.

Keep the transaction identifier and the miner hotkey it was intended to
credit. The [deposit helper](../noah/deposits/assistant/USAGE.md) can track its own sends,
but its credited status compares supplied totals rather than proving which
individual receipt earned the credit. Once daily input
and score files are available, [recompute the score](VERIFYING_SCORES.md)
and compare the recorded receipt with the chain.

Read the [deposit notices](DEPOSIT_NOTICES.md) before transferring stake.
