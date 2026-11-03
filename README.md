# Noah & the Ark — Bittensor Subnet 82

Noah scores registered miners on eligible NOAH earnings and qualifying
contributions to the Ark. The Noah Validator reads finalized chain records,
calculates daily scores, and publishes weights. The Ark Validator is a separate
hotkey to which contributed stake must be delegated.

Miner participation takes place on Bittensor Finney mainnet, netuid 82.
Start with the [miner quickstart](docs/MINER_QUICKSTART.md), and check the
[official Noah website](https://subnet82.com/) for current network updates
before signing a transaction.

## Start here

| Goal | Guide |
|---|---|
| Become a miner | [Miner quickstart](docs/MINER_QUICKSTART.md) |
| Read the participation rules | [Subnet 82 participation](docs/PARTICIPATION.md) |
| Create or protect a wallet | [Miner wallet setup](docs/MINER_SETUP.md) |
| Deposit through a delegate | [Miner proxy signing](docs/MINER_PROXY.md) |
| Test conversion followed by mixed deposits | [Mixed deposit test workflow](docs/MINER_MIXED.md) |
| Check official wallet addresses | [Mainnet address reference](docs/reference/WALLET_ADDRESSES.md) |
| Check the Ark addresses and qualifying receipts | [Contribution rules](docs/CONTRIBUTIONS.md) |
| Understand daily scores | [Scoring](docs/SCORING.md) |
| Reproduce a published score | [Score verification](docs/VERIFYING_SCORES.md) |
| Review the validator | [Validator guide](docs/VALIDATOR_QUICKSTART.md) |
| Run repository checks | [Testing](docs/TESTING.md) |
| Trace the implementation | [Architecture and review map](docs/REVIEW.md) |

New to Bittensor? The [glossary](docs/GLOSSARY.md) explains the keys, stake,
weights, and Noah's two validator roles.

## Miner participation

A new miner registers a hotkey on Subnet 82. **No upfront NOAH purchase or
Ark deposit is required for starter eligibility.** Registration charges and
transaction fees still apply. Starter entry is active from UTC day 1324
(November 3, 2026), after cutoff block **9,421,694**.

Registration does not guarantee immediate weight. The miner must appear in a
completed UTC-day roster and pass finalized eligibility/allocation checks.
Eligible newcomers share a small starter pool, subject to wallet/hotkey lifetime
allowances; no payout amount is guaranteed. Read [starter entry](docs/STARTER_ENTRY.md)
for limits, preserved historical credit and the activation commitments.

There is no Noah miner daemon or axon to run. Starter earnings count toward the
same **50% target for eligible spendable earnings** as other qualifying earnings.
After the starter allowance is exhausted, ordinary contribution-based scoring
determines rewards. Early contributions are retained; starter and ordinary
weights are not stacked.

The optional [deposit helper](noah/deposits/assistant/USAGE.md) is for ongoing
contributions, not registration or an entry payment. It previews each proposed
send, requires exact confirmation, and checks its receipt. It neither buys NOAH
nor schedules sends. A qualifying contribution identifies the miner, transfers
stake ownership to the Ark and arrives delegated to the Ark Validator. Read the
[contribution rules](docs/CONTRIBUTIONS.md) and [deposit notices](docs/DEPOSIT_NOTICES.md)
before choosing to send. A finalized receipt and credited score are separate checks.

## Inspect the calculation

After `./scripts/install.sh`, run this wallet-free saved example:

```sh
.venv/bin/python -m noah.scoring.audit_published \
  fixtures/shared/steady-at-target.json \
  fixtures/shared/out-python/steady-at-target.score.json
```

`AGREES` means the saved output follows from the supplied inputs under this
code version. It does not verify those inputs against the chain or prove that
weights were published. See [score verification](docs/VERIFYING_SCORES.md).

The mainnet settings are in
[mainnet-production-starter-25bps-v1.json](docs/parameters/profiles/mainnet-production-starter-25bps-v1.json).
Public role addresses are in the [address reference](docs/reference/WALLET_ADDRESSES.md).
Check current chain state and announcements before using either for a live
transaction. Code and tests alone do not establish an operating validator.

For optional local lifecycle diagnostics, see [application events](docs/APPLICATION_EVENTS.md).
Event capture is best-effort and is separate from chain verification.

The source is distributed under the [MIT License](LICENSE). See
[Security](SECURITY.md) for private vulnerability reporting and key handling,
and [Contributing](CONTRIBUTING.md) to propose a change.
