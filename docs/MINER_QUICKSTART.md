# Miner quickstart: Finney mainnet

> The current release candidate is V2.0, internally identified as `points-204040-v5` (policy/5). Historical policy identifiers remain unchanged for verification.

> **Review candidate:** `points-204040-v5` uses ordinary onboarding with no starter pool or entry payment. Verify activation before relying on these candidate economics. See [ordinary scoring](ORDINARY_SCORING.md).

This is the participant setup for Noah on Bittensor **Finney mainnet, netuid
82**. It uses a public chain RPC and your own local wallet. There is no Noah
miner daemon or axon to expose: registration and Ark contributions are signed
transactions, and the Noah Validator scores finalized chain activity daily.

Check current network information on the
[official Noah website](https://subnet82.com/) before registering, buying
stake, or sending to the Ark. An Ark contribution moves stake ownership and is
not withdrawable. Read the [deposit notices](DEPOSIT_NOTICES.md) before signing.

## 1. Install and choose an RPC

From the public source checkout, run `./scripts/install.sh`. It creates
`.venv-validator` with the deposit helper, chain client, and a compatible
`btcli`. It installs the **`noah-miner`** command in that environment.
The commands below use that environment's CLI. Check its version and
help against these commands before using funds.

```sh
git clone https://github.com/Vora-Inc/NOAH.git
cd NOAH
./scripts/install.sh
export NOAH_SOURCE="$PWD"
export PATH="$NOAH_SOURCE/.venv-validator/bin:$PATH"
btcli --version
```

Check the installed miner command:

```sh
noah-miner --help
```

[Bittensor's network list](https://guides.learnbittensor.org/concepts/bittensor-networks)
uses `finney` as the `btcli` name for mainnet and lists
`wss://entrypoint-finney.opentensor.ai:443` as a Finney WebSocket RPC. The
commands below use `--network finney`. The Noah deposit assistant needs the
**full RPC URL** in its INI file; it does not translate `finney` into one.
Public RPC availability and submission policy can change.

Before funding or signing, verify the endpoint's block-0 hash. The expected
Finney genesis in this release is
`0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03`:

```sh
.venv-validator/bin/python <<'PY'
from async_substrate_interface import SubstrateInterface

expected = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
connection = SubstrateInterface(url="wss://entrypoint-finney.opentensor.ai:443", auto_discover=False)
actual = connection.get_block_hash(0)
print("genesis:", actual)
if str(actual).lower() != expected:
    raise SystemExit("STOP: this RPC is not the recorded Finney mainnet")
PY
```

The RPC is your route **into the shared Bittensor chain**. It does not have to
be operated by Noah. The validator reads finalized chain records independently,
so a transaction submitted through any working Finney RPC can be observed for
scoring once it finalizes.

## 2. Have a signing wallet

You need a local Bittensor wallet with a **coldkey you can unlock to sign**
and a **hotkey controlled by that coldkey**. Know both public addresses and
their local wallet and hotkey names. Keep recovery backups offline. A hotkey
registered on another subnet can be used here; step 3 checks whether it has
a Noah UID. Run signing commands on a trusted machine that holds your coldkey.

If you already have that wallet, use it. If you need to create or restore one,
or add a hotkey to an existing coldkey, follow [miner wallet setup](MINER_SETUP.md)
before continuing. Set these names to match **your** local wallet:

```sh
export NOAH_WALLET_NAME=noah-miner
export NOAH_HOTKEY_NAME=default
btcli --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" wallet show
```

Replace the example names when your wallet uses different names. The
assistant's `wallet_name` and `hotkey_name` must match them too.

## 3. Fund and register the hotkey

Fund your coldkey with **real mainnet TAO** through a method you trust. It
needs enough for the current registration charge and transaction fees. An
upfront NOAH purchase or Ark deposit is not required. Read the
live subnet and price before accepting a transaction:

```sh
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" query subnet --netuid 82
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" wallet balance
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" query uid --netuid 82
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" subnet burn-cost 82
```

If `query uid` already returns a number for this hotkey, skip registration.
Otherwise inspect the dry run, then register once if the cost and addresses
are right:

```sh
btcli --dry-run --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" subnet register --netuid 82
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" subnet register --netuid 82
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" query uid --netuid 82
```

At the confirmation prompt, `y` authorizes a **real** registration charge;
`n` cancels. If a submission times out or reports an uncertain result, read
the UID and coldkey balance before considering another attempt. A successful
registration assigns a UID; it does not itself guarantee immediate scoring or a payout.

## 4. Enter ordinary scoring

An eligible new miner begins with 0.2 points after the first completed-day
calculation with verified identity/history, successful publication and chain
application. No upfront deposit is required. There is no shared pool or allowance.
Actual earnings on UTC day D create a 50% obligation during D+1, assessed at its
close. Read [the complete examples](ORDINARY_SCORING.md).

## 5. Make ongoing contributions when appropriate

Contribute 50% of eligible earnings or equivalent qualifying foreign/root value.
Prepaid credit can cover the obligation; a daily consistency bonus additionally
requires a real payment that meets that day's obligation. Uncovered debt halves
the last compliant score on the first day and sets it to zero on the second.

Read the [contribution rules](CONTRIBUTIONS.md) and
[deposit notices](DEPOSIT_NOTICES.md). To make a contribution, follow the
[deposit helper guide](../noah/deposits/assistant/USAGE.md). Choose the amount from
your actual earnings/credit evidence. There is no fixed onboarding payment.
Keep enough free TAO for fees and review the spendable stake and reserve before
signing. Returned or locked registration collateral is not mining earnings.

The helper's ordinary deposit command transfers stake already owned by your
wallet. It does not automatically purchase tokens or schedule sends. Use a stable
contribution label and reconcile an uncertain result before considering a retry;
do not send again merely because scoring credit is pending.

Optional advanced paths: [proxy signing](MINER_PROXY.md) and
[mixed deposit test workflow](MINER_MIXED.md). These are contribution tools,
not steps required to register or enter ordinary scoring. Experimental purchase
and conversion flows retain their documented network restrictions.

## Optional Mainnet contribution configuration

Use this only when you choose to make an ongoing contribution. It is not needed
to register or enter ordinary scoring. `init-config` writes a **generic test
network** example; do not send using its defaults. Create your local file:

```sh
noah-miner --config noah-mainnet-deposit.ini init-config
```

Edit that file with the following network mapping and your own wallet details.
Keep each comment on its own line. A label alone does not select a network.

| INI setting | Mainnet value |
|---|---|
| `[miner] network` | `mainnet` (label only) |
| `endpoint` | Your verified Finney WebSocket RPC URL from step 1 |
| `netuid` | `82` |
| `genesis_hash` | `0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03` |
| `miner_hotkey`, `coldkey` | Your registered hotkey and owning coldkey public addresses |
| `wallet_name`, `hotkey_name` | Your local wallet and hotkey names from step 2 |
| `stake_position` | `miner` when contributing earnings staked to your miner hotkey |
| `[ark] ark_coldkey` | `5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3` |
| `designated_validator_hotkey` | `5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV` |
| `[deposit] mode` | `custom` for an individually reviewed amount |
| `amount_noah` | Your chosen ongoing contribution, based on actual earnings and credit |
| `reserve_noah` | Your chosen amount to retain; the generic example reserves one NOAH |
| `[state] dir` | A durable local ledger directory dedicated to this miner/network |

Verify the shared destination against the [address reference](reference/WALLET_ADDRESSES.md).
Do not copy the example test subnet or leave the genesis unpinned. For percent or
maintenance modes, supply complete earnings and credited-contribution evidence as
described in the [helper guide](../noah/deposits/assistant/USAGE.md). Returned
registration collateral is not earnings; only actual preserved historical entry
credit is excluded from ongoing contributions.

Validate and preview without signing:

```sh
noah-miner --config noah-mainnet-deposit.ini check-config
noah-miner --config noah-mainnet-deposit.ini deposit --preview
```

The preview contacts the chain. Stop on an unexpected network, identity, amount
or refusal. An actual send requires the helper's separate confirmation; follow
the [ongoing contribution procedure](../noah/deposits/assistant/USAGE.md#3-choose-an-ongoing-contribution).
