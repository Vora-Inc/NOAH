# Miner quickstart: Finney mainnet

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
needs enough for the current registration charge, acquiring at least the
entry contribution's spendable NOAH stake, and transaction fees. Read the
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
registration assigns a UID; it is not the Ark entry contribution.

## 4. Acquire spendable NOAH stake

Check your position before buying stake. Registration collateral may appear
in the wallet's total NOAH but may not be transferable to the Ark. The entry
needs **10 spendable NOAH**, plus any reserve you set in the helper. With the
1 NOAH reserve shown below, have at least **11 spendable NOAH** and separate
free TAO for network fees. The reserve stays in your wallet; it is not another
entry charge.

```sh
btcli --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" wallet overview
btcli --dry-run --network finney --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" tx add-stake --netuid 82 --amount-tao YOUR_REVIEWED_AMOUNT
```

Replace `YOUR_REVIEWED_AMOUNT` with an amount you choose after checking the
live quote and available TAO. If the preview is right, repeat the command
without `--dry-run`, then read `wallet overview` again. This buys stake for
your miner hotkey; it does not yet move ownership to the Ark.

## 5. Configure the deposit assistant

Work in a private directory on your trusted signing machine. Keep this mainnet settings
file and ledger separate from any test or rehearsal files:

```sh
mkdir -p "$HOME/noah-mainnet-miner"
cd "$HOME/noah-mainnet-miner"
noah-miner --config noah-mainnet-deposit.ini init-config
```

Edit **all** of the generated `[miner]`, `[ark]`, `[deposit]`, `[evidence]`,
and `[state]` sections. In particular, the generated local-node endpoint and
test subnet values are examples, **not mainnet defaults**. Use the public
addresses below only after checking them against Noah's current official
announcement and on-chain records:

```ini
[miner]
network = mainnet
endpoint = wss://entrypoint-finney.opentensor.ai:443
netuid = 82
miner_hotkey = YOUR_REGISTERED_HOTKEY_ADDRESS
coldkey = YOUR_COLDKEY_ADDRESS
wallet_name = noah-miner
hotkey_name = default
genesis_hash = 0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03
stake_position = miner

[ark]
ark_coldkey = 5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3
designated_validator_hotkey = 5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV

[deposit]
mode = custom
amount_noah = 10
reserve_noah = 1

[evidence]
file = ./noah-mainnet-evidence.json

[state]
dir = ./noah-mainnet-deposit-state
```

If you use a different trusted, submission-capable Finney RPC for the helper,
put its full URL in `endpoint` and verify that endpoint's genesis first.
Replace the wallet names and both miner addresses with your own values from
step 2; do not copy another miner's keys into the wallet.
The assistant reads and submits through `[miner] endpoint`; it does not read
`btcli`'s network setting. `network = mainnet` is a label and
validator check, not an automatic connection selector. The pinned
`genesis_hash` makes the assistant refuse a different chain before signing.
The Ark addresses above are from the
[mainnet address reference](reference/WALLET_ADDRESSES.md), which must be
checked again before use. The Ark coldkey receives ownership;
the designated validator hotkey receives delegation. Neither is your miner
hotkey.

`stake_position = miner` is for NOAH stake under your miner hotkey. If you
already hold stake under a different validator hotkey, read the
[helper's stake-position rules](../noah/deposits/assistant/USAGE.md#2-write-the-settings-file)
before editing that field. The helper does not buy tokens.

## 6. Preview, send once, and verify

Run the preview from the directory containing your settings. It sends
nothing and should show the correct chain, miner, Ark, amount, available
spendable stake, and reserve:

```sh
noah-miner --config noah-mainnet-deposit.ini preview
```

Stop if the preview refuses, the chain or addresses differ, the required
transfer switch is off, or the proposal is not what you intend. The helper
does not estimate the TAO network fee. When the preview matches your intended
transfer, the manual send is:

```sh
noah-miner --config noah-mainnet-deposit.ini deposit --label entry
```

Check the receipt:

```sh
noah-miner --config noah-mainnet-deposit.ini status
```

`noah-miner deposit` requires an exact typed confirmation and your wallet password.
An on-chain `finalized` receipt means the transfer qualified in the helper's
readback; scoring credit still needs a published daily record. If the outcome
is uncertain, run `status` before trying any further transfer. Never resend
the 10 NOAH entry merely because credit is pending.

Keep the transaction identifier and compare its amount, ownership transfer,
delegation, and miner attribution against finalized chain records. Then check
the next applicable published contribution and score record as described in
[score verification](VERIFYING_SCORES.md). A `credited` helper status compares
totals you supplied; it is not proof that this particular receipt earned credit.

## 7. Continue after entry

The mainnet profile specifies a **50% ongoing contribution target** for
eligible earnings. This is a scoring target, not an automatic wallet debit.
Daily contributions are requested. The helper supports `custom`, `fixed`,
`percent`, and `maintenance` amount modes. Its `percent` and `maintenance`
modes require a complete external
earnings and credited-contribution evidence file; see the
[deposit helper guide](../noah/deposits/assistant/USAGE.md). Each send is
manual and needs confirmation. Review the finalized receipt, attributed
contribution, day-specific score, published weight, and actual emissions as
separate results. Do not schedule `noah-miner deposit` unattended. The
[contribution rules](CONTRIBUTIONS.md) explain advance and catch-up credit,
including shared wallets.

## Inspect your active settings

Each miner uses one active INI. Its `[miner]`, `[ark]`, `[deposit]` and `[state]`
sections hold the base settings; optional `[signing]`, `[signer]` and `[mixed]`
sections belong in that same file. Separate files are useful for different miners
or disposable test scenarios, not required for different actions. Combined proxy
and mixed execution remains unsupported.

Show the actual configured sections, even while placeholders remain:

```sh
noah-miner --config noah-mainnet-deposit.ini config
```

This is a display-only INI view: credentials and endpoint URL details are redacted;
comments are omitted. It neither unlocks keys nor contacts the chain. Do not store
private keys or passwords in the INI. Use `config --json` for validated JSON settings
or `check-config` to validate without contacting the chain.

Edit your INI using `VISUAL` or `EDITOR` (default `nano`):

```sh
noah-miner --config noah-mainnet-deposit.ini config edit
```

The command edits a private draft, validates it locally, and saves a backup
before replacing your settings. Invalid edits leave the original unchanged.
It does not contact the chain or sign. The INI must be a writable regular file;
for a managed installation, have its operator update the source settings.


## Optional proxy signing

The same deposit command can use a delegate while the real coldkey stays
offline. Keep your original owner and miner hotkey in `[miner]`; choose proxy
signing in the same active INI. See [proxy deposits](MINER_PROXY.md) for the one-time
delegation and constrained signer setup.

```sh
noah-miner --config noah-mainnet-deposit.ini deposit --preview
```

```sh
noah-miner --config noah-mainnet-deposit.ini deposit --label entry
```

## Conversion followed by mixed deposits

The `convert-deposit` command is currently restricted to disposable private-test
work. Production mixed deposits remain disabled pending the approved policy.
See the [mixed deposit guide](MINER_MIXED.md) for settings and the three-step
workflow. Use the active INI of a disposable private-test miner, not the mainnet
INI above. Each target still has one INI containing all of its settings:

```sh
noah-miner --config /path/to/private-test/deposit.ini convert-deposit --preview --label mixed-test-01
```

### Mixed workflow outcomes

`delivery_complete: true` means all three transfers were verified at finalized
blocks; it does not establish scoring credit.

A `not_submitted` step with `error_code: quote_expired` means its quote expired
before the submission call. A conversion refused this way closes that label
permanently, preserves its record, and sends neither deposit. Review a fresh
preview and choose a new label for a new attempt. A later deposit leg refused
before submission keeps earlier completed steps; rerunning the original label
asks for approval and attempts only the unsent leg.

An `uncertain` step remains blocked. Check status using the **original label**;
do not change labels, delete state, or assume a timeout means failure. The
output records the last phase and whether submission was started. A saved
transaction hash alone does not prove that the node received it.

## Fixed purchase using free TAO

For disposable private-test accounts, `purchase-deposit` buys the configured
asset using a fixed TAO cap, deposits the fixed NOAH amount, and then transfers
only the purchased asset quantity established by its finalized receipt.
See the [purchase settings and proxy requirements](../noah/deposits/assistant/USAGE.md#fixed-free-tao-purchase-rehearsal).
Production execution remains disabled pending live verification and approval.

```sh
noah-miner --config noah-deposit.ini purchase-deposit --preview --label purchase-test-01
```

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label purchase-test-01
```

```sh
noah-miner --config noah-deposit.ini purchase-deposit --status --label purchase-test-01
```
