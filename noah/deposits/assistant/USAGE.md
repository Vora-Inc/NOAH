# Noah deposit helper: newcomer guide

A terminal tool you run on your own computer. It works out a proposed Ark contribution, shows
it in plain English, and sends it only after you type an exact confirmation. Your own local
bittensor wallet signs. Keep recovery words in your wallet tooling; do not put them in the
helper configuration, evidence files or diagnostic reports.

> A transfer made with transfer_stake_and_hotkey is attributed to your miner hotkey from the
> transfer itself, even when it is sent inside a proxy, multisig or batch. A plain transfer_stake is
> attributed only through a remark in a top-level batch_all; a plain transfer_stake with no
> remark, or one whose remark sits inside a proxy, multisig, Utility.batch or another wrapper
> where it is not read, names no miner hotkey, so the deposit is recorded as unattributed, with
> a reason, and credits nobody (the chain itself shows no error). This helper sends NOAH
> directly, using transfer_stake_and_hotkey (stake_position = miner) or a top-level batch_all
> that carries the remark (stake_position = validator), with optional proxy signing only for the miner position. The entry
> deposit must name your miner hotkey the first time; an unnamed attempt is recorded as
> unattributed and does not count as your entry.

Use it only on a network you have been told to use. Nothing here has been proven on the live
(mainnet) chain.

## 1. Install

From the Noah source root:

```
./scripts/install.sh
```

This builds `.venv-validator`, which installs `noah-miner` with the chain client
and wallet library the tool needs. Activate that environment first:

```sh
source .venv-validator/bin/activate
```

All commands below run from the Noah source root as:

```
noah-miner --config noah-deposit.ini COMMAND
```

`--config` defaults to `noah-deposit.ini` in the current folder. `--help` lists the commands.

## 2. Write the settings file

```
noah-miner --config noah-deposit.ini init-config
```

It writes an example file (it never overwrites an existing one) and prints notes about the
endpoint and the two stake positions. Edit the file. Keep every comment on its own line, because
text after a value is read as part of the value. Do not put recovery words in it: any field named
like a secret (mnemonic, seed, secret, private, password, phrase, recovery) makes the tool refuse
to start.

| Key | Meaning |
| --- | --- |
| `[miner] network` | A label for your records, for example `mainnet`. The endpoint and pinned genesis select and verify the chain. |
| `endpoint` | The chain node as `ws://host:port`. The default `ws://127.0.0.1:9944` is a node on this computer. Point it at a node you run or trust: it supplies every number the tool reads. |
| `netuid` | The subnet number. |
| `miner_hotkey` | Your registered miner hotkey address. |
| `coldkey` | Your coldkey address. It owns the stake and signs. |
| `wallet_name`, `hotkey_name` | Names of your local bittensor wallet. |
| `wallet_path` | Optional. Only if your wallets are not in `~/.bittensor/wallets`. |
| `genesis_hash` | Optional but recommended. The chain's block-0 hash. If set, the tool refuses to run on any other chain. |
| `stake_position` | `miner` or `validator`, see below. Default `miner`. |
| `[ark] ark_coldkey` | The official Ark address. |
| `designated_validator_hotkey` | The designated Ark Validator. Required (or set the environment variable `NOAH_VALIDATOR_HOTKEY`). It must differ from your miner hotkey. |
| `[deposit] mode` | `custom`, `fixed`, `percent` or `maintenance`, see section 4. |
| `percent` | For `percent` mode: above 0 up to 100. 50 is the suggested value. |
| `amount_noah` | For `custom` and `fixed` modes. |
| `cap_noah` | Optional maximum per transaction. |
| `minimum_noah` | Optional. Deposits smaller than this are skipped. |
| `reserve_noah` | Amount kept back from what can be sent. If the key is left out, 0. The file written by `init-config` sets it to 1, so a newcomer starts with a 1 NOAH reserve. |
| `[evidence] file` | Path to the evidence JSON file (section 5). |
| `[state] dir` | Folder for the ledger file `deposits.db`. Default `./noah-deposit-state`. |

### Which `stake_position`

- `miner`: your NOAH is staked to your own miner hotkey, where mining earnings land. The helper
  sends one `transfer_stake_and_hotkey` to the Ark and re-delegates it to the Ark Validator in the
  same step. Choose this for ordinary earnings.
- `validator`: your NOAH is already staked to the designated Ark Validator, and you own the miner
  hotkey with the same coldkey. The helper sends a top-level `batch_all` with the Ark transfer and
  a note naming your miner hotkey. Before sending it checks that your coldkey owns the miner
  hotkey; if not, it refuses. This shape works before the Ark Validator hotkey is registered on
  the subnet: it reads the stake parked on that hotkey even when the chain reports the hotkey as
  not registered. If nothing is staked there yet, the ceiling is zero and it says so.

The `deposit` command only moves NOAH that is already staked. The separate
private-test purchase command below can buy stake using a fixed free-TAO budget.

## 3. The 10 NOAH entry contribution and the first deposit

The 10 NOAH entry contribution is a separate requirement. It is never part of the ongoing
percentage or maintenance calculations. This helper does not check it.

To make a first contribution with the helper, first stake the NOAH to the right place with your
usual wallet tools (your miner hotkey for `miner`, the Ark Validator for `validator`). Then set
`mode = custom` and `amount_noah = 10` and run it with an explicit label:

```
noah-miner --config noah-deposit.ini deposit --label entry
```

Whether the validator side counts that deposit as your entry is decided by the validator reader,
and this path has not been proven end to end as an entry deposit. Confirm against the published
records.

## 4. Amount modes

- `custom`: a one-time amount you type in the file.
- `fixed`: the same amount each time you run it.
- `percent`: your `percent` of eligible earnings for the measured period. Below 50 the tool
  warns that it does not lower the subnet requirement and may reduce your score.
- `maintenance`: only what is still needed so that credited contributions reach 50% of
  cumulative eligible earnings.

If a limit (cap or available funds) prevents the full amount in `percent` or `maintenance`, the
tool proposes the smaller amount and says the goal is not reached. For `custom` and `fixed` it
refuses instead of shrinking your amount.

## 5. Evidence file

The tool does not look up your earnings itself. You (or whoever publishes your records) supply a
JSON file at `[evidence] file`.

For `custom` and `fixed`, the file is optional. Without it the deposit is still sent, but it can
never be shown as credited. The only field used is the credited total before you send:

```
{ "credited_contributions_rao": "300000000000" }
```

`credited` status needs this field, measured before the deposit and excluding the 10 NOAH entry
contribution.

For `percent` and `maintenance` the file is required and must say its history is complete:

```
{
  "through_day": "2026-09-29",
  "history_complete": true,
  "period_start_day": "2026-09-28",
  "period_eligible_earnings_rao": "100000000000",
  "cumulative_eligible_earnings_rao": "1000000000000",
  "credited_contributions_rao": "300000000000"
}
```

`percent` uses `period_eligible_earnings_rao` and `period_start_day`; `maintenance` uses
`cumulative_eligible_earnings_rao` and `credited_contributions_rao`. A missing field or
`history_complete` not `true` is a visible refusal, never read as zero. Amounts are whole rao
(1 NOAH = 1,000,000,000 rao). Unknown field names are refused.

## 6. Preview, send, status

Preview calculates and explains. It sends nothing:

```
noah-miner --config noah-deposit.ini preview
```

Output shape: a header listing your miner hotkey, your coldkey, the official Ark and the
designated Ark Validator as separate lines; how the amount was chosen; the proposed deposit,
available funds, reserve and what is left; a note that the network fee is paid in TAO and is not
estimated; how it is sent; and any warnings.

Send:

```
noah-miner --config noah-deposit.ini deposit
```

It first checks earlier deposits against the chain, shows the same summary, then asks you to
type exactly `SEND <amount> NOAH` (for example `SEND 200 NOAH`). Anything else cancels with
nothing sent. Only then does it open your wallet (it may ask for your wallet password), sign,
submit and check the result. For `custom` and `fixed`, `--label NAME` marks a deliberate repeat.

Status checks the chain and shows each deposit:

```
noah-miner --config noah-deposit.ini status
```

States, in order of how much they prove:

| State | Meaning |
| --- | --- |
| awaiting approval / approved | Nothing sent yet. |
| submitting | Being sent. If it stays here after a crash, run `status`. |
| submitted | In a block. This is not proof of credit. |
| finalized | Final on chain and read back as a qualifying deposit. Credit not yet confirmed. |
| credited | Your evidence shows the credited total rose by at least the deposit. |
| uncertain | Outcome unknown. No new deposit starts until it is checked against the chain. |
| unqualified | Landed on chain but would not be credited (see the note). Do not repeat blindly. |
| declined | Confirmation was declined; nothing sent. |
| failed | The attempt failed. An included failed transaction may still incur a network fee; inspect the recorded reason. |
| abandoned | Reconciliation found no matching deposit after the transaction validity window. Inspect chain records before retrying. |

Credit confirmation compares credited totals, so an unrelated credit of the same size could
confirm a deposit early.

## 7. Resolve

If a deposit was sent without an evidence figure, it stays `finalized` and a later `percent` or
`maintenance` run refuses with the exact command to use. You supply the credited total (in rao)
you measured just before that deposit, from the published records:

```
noah-miner --config noah-deposit.ini resolve ID_PREFIX --credited-before-rao NUMBER
```

You must type the confirmation it prints. The tool cannot verify your number; a figure that is
too low can mark the deposit credited too early. It refuses a figure above the credited total
shown now or below an earlier deposit's starting figure. The ledger records it as miner-supplied.

## 8. Troubleshooting

| What you see | What it means and what to do |
| --- | --- |
| "cannot reach the chain node at ..." | The endpoint is wrong or the node is down or blocked. Fix `endpoint`, start the node, check your network, then run `status`. A deposit in progress is recorded safely. |
| "Connected chain's genesis ... is not the one in your settings" | You are on a different network than `genesis_hash`. Do not continue until you know which is right. |
| "designated Ark Validator hotkey is not set" | Set `designated_validator_hotkey` or `NOAH_VALIDATOR_HOTKEY`. |
| State `unqualified`, note "staked to ..., not the designated Ark Validator" | The receipt arrived at the Ark but earns no mining credit. Do not repeat the same deposit blindly. |
| State `uncertain` | The send may or may not have landed. Run `status`. It stays blocking until the chain shows it, or until the finalized chain is well past the transaction's validity window, when it becomes `abandoned` and a retry is allowed. |
| "This coverage already has a deposit ..." | Same amount, same label (or same measured day for percent and maintenance). For `custom` or `fixed`, use `noah-miner deposit --label NEW_NAME` only if you really mean a second deposit. |
| "An earlier deposit ... is still ..." | Run `status` to bring it up to date before starting another. |
| "pending_unknown" refusal | Use `resolve` (section 7). |
| "Your coldkey does not currently own the miner hotkey" | Only for `stake_position = validator`. The wallet signing is not the owner of the miner hotkey. |
| "Nothing is staked on the designated Ark Validator for this coldkey yet" | Only for `stake_position = validator`. Stake NOAH to the designated Ark Validator with this coldkey first. The Ark Validator hotkey does not need to be registered. |
| "SubtokenEnabled is false for subnet" | The subnet does not allow stake transfers yet. The chain would revert the transaction after you sign it and may still charge a network fee, so the helper stops before asking. Nothing was signed or sent. Wait until the subnet owner enables it, then run `preview` again. |
| "Stake transfers are not permitted on subnet" | The subnet's `TransferToggle` is false. Same effect and same early stop as above. |
| "setting for subnet" ... "could not be read as a clear true or false" | The node answered with something other than a clear true or false, so the helper cannot confirm the transfer will work and refuses rather than guess. Check `endpoint` points at the right node and try again. |
| "history ... not confirmed complete" | The evidence file lacks `history_complete: true` or a needed figure. |

## 9. Not in this helper

- Subnet 82 transfers while its `SubtokenEnabled` setting is false. While it is false,
  `transfer_stake` and `add_stake` fail on chain with `SubtokenDisabled` before any hotkey check,
  whichever shape you use. The helper checks the chain's effective `SubtokenEnabled` and the
  subnet's `TransferToggle` (the stored value, or the runtime's own default when nothing is
  stored) and refuses before signing, in `preview` as well as `deposit`, with nothing signed or
  sent. These are checks the transaction itself needs, not hotkey registration checks.
- No proxy setup wizard. A plain transfer_stake whose remark sits inside a proxy, multisig,
  Utility.batch or another wrapper names no miner hotkey: the deposit is recorded as unattributed
  with a reason and credits nobody. This helper never builds that form.
- No schedule, and so no pause, resume or edit of a schedule. Every run is manual.
- No hardware-wallet path. Direct mode signs with a local bittensor wallet; proxy mode uses a separately operated delegate signer.
- No setup wizard. `init-config` writes an example file you edit.
- The evidence file is hand-supplied. Nothing builds your earnings or credited figures from the
  chain for you.
- The ordinary deposit command does not buy stake. The separate experimental
  fixed free-TAO purchase command below estimates fees before signing.

## Signing in these examples

The examples use direct signing with your own local wallet. Keep
`[miner] coldkey` as the real stake owner and `miner_hotkey` as the miner
identity. Never put key material in the INI file.

## Fixed free-TAO purchase rehearsal

`purchase-deposit` is an experimental **pinned private-test-chain**
command, with an optional separately operated proxy signer. It does not calculate
earnings or assign scoring credit. It does not sell NOAH. Mainnet, public testnet,
root/subnet 0 and multiple purchase assets are refused until those paths have separate implementation and
runtime proof.

Use the usual owner/miner and Ark fields in one INI. Set `[deposit] mode = fixed`
and `amount_noah` to the exact NOAH transfer amount (`0` skips that leg). Add:

```ini
[purchase]
test_only = true
foreign_netuid = 3
foreign_ark_hotkey = REPLACE_WITH_REVIEWED_RECEIVING_HOTKEY
max_tao_rao = 10000000
max_slippage_bp = 100
quote_max_age_blocks = 5
max_fee_rao = 10000000
runtime_code_hash = REPLACE_WITH_REVIEWED_RUNTIME_CODE_HASH
spec_version = REPLACE_WITH_REVIEWED_RUNTIME_VERSION
```

The fixed TAO input is a hard cap. The 1% execution-price limit does not enlarge
it. Fees are separate; the command checks the purchase and estimated sequence
fees while preserving a positive free balance and the runtime's existential
deposit. A live price quote is distinct from any historical price the caller
used to choose the fixed budget.

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label example-01 --preview
```

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label example-01
```

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label example-01 --status
```

The sequence is `add_stake_limit` with partial fill disabled, then the fixed
NOAH transfer, then a foreign transfer of **only the actual alpha from the
finalized purchase event**. Existing foreign stake and estimated output are
never used as the foreign deposit amount. A failed or unresolved purchase stops
the transfers. These are separate transactions: later failure can leave a
partial delivery. Keep the original label, configuration and journal for status
and recovery. An uncertain transaction is never automatically resent; a failed
transaction remains blocked for inspection. Do not erase state or change the
label to bypass it.

A proxy attempt whose saved quote-expired failure proves it stopped before the
signer's PREPARE request can be permanently closed with its original configuration
and label:

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label example-01 --cancel-unsubmitted
```

This refuses reserved, submitted or uncertain transactions. It never refunds or
retries a sent transaction. The signer permanently blocks the old plan; use a
new reviewed plan and configuration afterward. If closure is interrupted, repeat
this command with the original settings and journals to recover the same proof.

An original proxy purchase marked uncertain can refresh its quote only when the
signer proves submission never began:

```sh
noah-miner --config noah-deposit.ini purchase-deposit --label example-01 --recover-pre-submit
```

This preserves the plan, journal identity, reservation token and signed-hash
history. It never signs or sends; an entered or unknown submission is refused.
Inspect the result before using the ordinary command with the same label.

All workers spending the same owner must share the canonical owner ledger and
purchase journal. The shared lock and durable reservation prevent parallel
nonce use and bypassing an unresolved deposit through another command. This is
not coordination with independent wallets or processes that ignore that state.

### Proxy purchase rehearsal

Keep the real owner and original miner hotkey in `[miner]`. In the existing
`[signing]` section, retain `proxy_type = Transfer` and add
`purchase_proxy_type = Staking`. Both zero-delay delegations must exist for the
**same delegate**: Staking authorizes `add_stake_limit`; Transfer authorizes the
attributed deposits. The helper never substitutes an Any proxy. The owner pays
for the purchase, and the delegate pays transaction fees. Neither account may
be drained below positive funds or runtime survival requirements.

Set a stable `plan_id` in `[purchase]`, such as `example-day-01`. The separately
operated signer's trusted INI must contain that exact fixed plan and receiver.
Its `[signer]` section adds `max_total_tao_rao`, a lifetime cumulative purchase
limit. Existing `max_amount_rao` and `max_total_rao` remain NOAH limits. These
lifetime limits are distinct from the plan's exact TAO input and do not grant
the client permission to increase that input.

The signer exposes only purchase, fixed NOAH delivery and exact acquired-alpha
delivery requests. It derives the foreign amount from its own finalized
purchase receipt. Changing CLI labels cannot repeat a plan's purchase or its
delivery legs. Give each new, reviewed fixed plan a distinct `plan_id`; retain
all earlier state. All signer processes sharing an owner or delegate must mount
the same signer journal so nonce use and uncertain submissions remain serialized.

Use the same `purchase-deposit` commands above. Proxy preview checks public
delegations, balances and trusted signer limits without retrieving signing
material or creating a transaction reservation. An unavailable signer leaves the
preview blocked. Runtime-dependent
proxy purchase and receipt behavior still requires disposable-wallet live proof
before any production authorization.

Proxy submission first obtains a durable, never-submitted signer reservation.
If the signer disconnects, retry the same command and label: a matching retained
reservation can establish that no signing request started. Missing or replaced
signer state cannot establish that fact and remains blocked for investigation.
Changing an address's SS58 network prefix does not create a new owner, miner or
plan. Full fixed NOAH amount and lifetime capacity are checked before buying.
