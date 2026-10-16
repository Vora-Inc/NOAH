# Deposit through a proxy wallet

Use this after completing the [miner quickstart](MINER_QUICKSTART.md).
The original coldkey owns the stake, the original hotkey identifies the miner,
and a separate delegate signs the transaction. Keep both original identities
in `[miner]`. Never import a Ledger recovery phrase onto the signing computer.

## One-time delegation

Create a separate delegate wallet if you do not already have one. Secure its
backup and give it enough free TAO for transaction fees. The real owner must
authorize a zero-delay `Transfer` proxy on chain using its existing trusted
signing method. For a Ledger owner, use a Ledger-compatible signing interface.
See Bittensor's [proxy setup guide](https://guides.learnbittensor.org/keys/proxies/working-with-proxies).

Check the delegation without signing:

```sh
btcli --network finney proxy list --coldkey YOUR_REAL_COLDKEY_ADDRESS
```

The delegate, proxy type and delay must match your configuration. This helper
supports `stake_position = miner` and delay zero. It refuses validator-position
proxy deposits because their attribution is not supported. It never substitutes
`Any` permission. Confirm the target runtime permits the exact deposit call
under `Transfer`; a proxy relationship alone does not establish that permission.

## Configure a bounded signer

Use your existing active deposit INI (called `noah-deposit.ini` below). Keep its original `[miner]` and
`[ark]` addresses. For this example use a fixed 10 NOAH deposit and add:

```ini
[signing]
mode = proxy
proxy_address = YOUR_DELEGATE_PUBLIC_ADDRESS
proxy_type = Transfer
delay_blocks = 0
secret_type = bittensor-wallet
signer_ref = wallet:my-delegate
signer_socket = /ABSOLUTE/PRIVATE/DIRECTORY/noah-signer.sock

[signer]
policy_id = my-miner
runtime_code_hash = REVIEWED_RUNTIME_CODE_HASH
spec_version = REVIEWED_RUNTIME_SPEC_VERSION
max_amount_rao = 10000000000
max_total_rao = 10000000000
max_fee_rao = YOUR_REVIEWED_MAXIMUM_FEE_IN_RAO
```

`my-delegate` is the local delegate wallet name. Its derived public address
must equal `proxy_address`; the real owner's private key is never opened.
Use an existing private directory for the socket. The signer uses a Unix
socket accessible only to its operating-system user, with no network listener.

Set the runtime code hash and version only after reviewing that runtime's proxy
permissions. A runtime change stops signing until those pins are reviewed again.
Limits use integer units: one NOAH or TAO is 1,000,000,000 base units. The amount
limit is per transaction, the total is cumulative, and the fee limit is TAO.
On-chain proxy permissions do not enforce the Ark destination or these spending
limits; the signer enforces them. Protect the delegate accordingly.

## Start the signer, preview and deposit

In a trusted terminal, activate the installed environment and start the signer:

```sh
noah-miner --config noah-deposit.ini signer
```

Leave that terminal open for any delegate-wallet password prompt. The signer
loads the delegate key only when an approved request reaches the signing step.
Use the same durable signer journal for all workers sharing an owner or delegate;
the default is `~/.noah/signer-state`. Keep that journal for retries and recovery.

In another terminal with the same environment active, preview:

```sh
noah-miner --config noah-deposit.ini deposit --preview
```

The preview checks the real stake, delegate fee balance and on-chain delegation.
It sends nothing and does not unlock the delegate wallet. Then submit the
reviewed deposit:

```sh
noah-miner --config noah-deposit.ini deposit --label entry
```

Verify its result:

```sh
noah-miner --config noah-deposit.ini status
```

Use the same label and journal when inspecting or retrying the same intended
deposit. An uncertain result must be reconciled before another send. Stop the
signer with Ctrl-C when finished. Ctrl-C (SIGINT) and SIGTERM stop accepting new
requests, let the current request finish, close the socket and connections, and
exit. A request waiting on the chain can delay shutdown; keep its journal and
inspect any uncertain result before restarting. Repeated signals do not cancel
or erase a transaction. Cleanup failures produce a nonzero exit status.
To inspect its uncertain receipts without loading keys:

```sh
noah-miner --config noah-deposit.ini signer --reconcile
```

A finalized attributed transfer and validator scoring credit are separate
checks. Follow the quickstart's receipt and score verification steps.
