> Version scope: this document retains predecessor rules and receipt/history details. For candidate `points-204040-v5`, [ordinary scoring](ORDINARY_SCORING.md) governs onboarding, contribution, bonuses and publication. Historical starter descriptions below do not activate a pool in V5.

# Subnet 82 participation

> **Current Mainnet entry policy:** Starter entry is active from UTC day 1324 (November 3, 2026), after cutoff block **9,421,694**. New miners do not need an upfront NOAH purchase or Ark deposit. Registration fees, eligibility checks and ongoing contribution rules still apply. See [starter entry](STARTER_ENTRY.md).

Noah miners participate on Bittensor Finney mainnet, netuid 82. Follow the
[miner quickstart](MINER_QUICKSTART.md) for wallet, registration, and Ark
contribution steps. Check the [official Noah website](https://subnet82.com/)
for current network updates before signing a transaction.

## Participation rules

| Requirement | Mainnet policy |
|---|---|
| Starter entry | No upfront NOAH purchase or Ark deposit; registration and eligibility checks still apply |
| Starter allocation | Shared limited pool with coldkey/hotkey lifetime allowances; no guaranteed payment |
| Ongoing target | 50% of eligible spendable earnings |
| Returned registration collateral | Excluded from mining earnings |
| Additional minimum holding | None in the mainnet profile |
| Qualifying contribution | Names the miner, transfers ownership to the Ark, and arrives delegated to the Ark Validator |
| Additional reports | Requested, with no effect on eligibility or scores |

The checked-in [mainnet profile](parameters/profiles/mainnet-production-starter-25bps-v1.json)
contains the scoring settings. Test fixtures can use other values and
addresses. See [contribution rules](CONTRIBUTIONS.md)
and the [mainnet address reference](reference/WALLET_ADDRESSES.md) before
sending funds.

## Check a transaction and its score

Before signing, verify the RPC's Finney genesis, netuid, your wallet addresses,
the live registration charge, the Ark receiving coldkey, and the designated
Ark Validator hotkey. Confirm that the subnet permits the intended stake
transfer and that your wallet has spendable NOAH and TAO for network fees.
The [miner quickstart](MINER_QUICKSTART.md) shows the checks and commands.

After a send, keep its block and transaction identifiers. A finalized Ark
contribution is separate from scoring credit. Check the attributed deposit,
daily score, submitted weight, and finalized chain result using the
[score verification guide](VERIFYING_SCORES.md). A local test or dry run
establishes only the behavior it exercised; it does not prove a transaction
or published weight exists on mainnet.
