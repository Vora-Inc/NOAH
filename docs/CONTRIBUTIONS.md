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

A qualifying NOAH contribution must arrive at the official Ark coldkey,
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

## Starter entry and ongoing contributions

Current Mainnet starter eligibility does not require an upfront NOAH purchase
or Ark deposit. Register normally and follow the [starter policy](STARTER_ENTRY.md).
Registration collateral is separate: its return is not earnings, and locked
collateral cannot fund a contribution.

The ongoing target is **50% of eligible spendable earnings**, including starter
earnings. Qualifying contributions accumulate with their actual receipt dates.
Early contributions are retained, but do not stack ordinary weight with starter
weight. After the starter allowance is exhausted, ordinary scoring determines
rewards. Advance and catch-up deposits still require the correct Ark destination,
Ark Validator delegation and miner attribution.

### Historical credit preserved at activation

Before the cutoff, the legacy rule required ten NOAH per miner hotkey. The frozen
snapshot preserves existing qualifying credit and its original dates; those same
tokens are excluded from ongoing credit exactly once. This historical accounting
explains the retained numeric field in the profile. It is **not** a payment
instruction for a new miner. Post-cutoff contributions are not used to create a
new legacy entry fee. See the [activation commitments](STARTER_ENTRY.md#mainnet-activation-and-reproduction).

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

That is the coverage calculation. Your score also depends on credit aging and
streak history. Clearing a debt does not turn every previous missed day into
an on-time contribution. The [scoring guide](SCORING.md) separates those parts.

## Other subnet tokens

The scoring engine accepts other-token contributions as a NOAH-equivalent
value. They can add matching credit, limited by the miner's NOAH credit.
They do not replace NOAH in the contribution-compliance calculation.

The chosen valuation policy uses the token's current price when it enters the
Ark, not an EMA or the lower of two prices. This guide covers NOAH
contributions; other subnet tokens require separate verification of their
deposit and valuation path.

## Checking a receipt

Check the actual stake amount received, not just a displayed TAO-equivalent
transfer amount. The reader joins the transfer and stake events from the
transaction to establish ownership and the received alpha amount.

Keep the transaction identifier and the miner hotkey it was intended to
credit. The [deposit helper](../noah/deposits/assistant/USAGE.md) can track its own sends,
but its credited status compares supplied totals rather than proving which
individual receipt earned the credit. Once daily input
and score files are available, [recompute the score](SCORING.md#check-a-score)
and compare the recorded receipt with the chain.

Read the [deposit notices](DEPOSIT_NOTICES.md) before transferring stake.
