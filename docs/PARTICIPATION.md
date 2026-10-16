# Subnet 82 participation

Noah miners participate on Bittensor Finney mainnet, netuid 82. Follow the
[miner quickstart](MINER_QUICKSTART.md) for wallet, registration, and Ark
contribution steps. Check the [official Noah website](https://subnet82.com/)
for current network updates before signing a transaction.

## Participation rules

| Requirement | Mainnet policy |
|---|---|
| Entry contribution | 10 spendable NOAH per miner hotkey, separate from ongoing credit |
| Ongoing target | 50% of eligible spendable earnings |
| Returned registration collateral | Excluded from mining earnings |
| Additional minimum holding | None in the mainnet profile |
| Qualifying contribution | Names the miner, transfers ownership to the Ark, and arrives delegated to the Ark Validator |
| Additional reports | Requested, with no effect on eligibility or scores |

The checked-in [mainnet profile](parameters/profiles/mainnet-production-v1.json)
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
