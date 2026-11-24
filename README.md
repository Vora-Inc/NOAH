# Noah & the Ark — Bittensor Subnet 82

> Mainnet uses V2.1 (`points-204040-v6`, policy/6) from November 24, 2026 at 00:00 UTC (scoring day 1345). V2 (`points-204040-v5`, policy/5) remains the policy for its earlier results.

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
| Understand daily scores | [Ordinary scoring](docs/ORDINARY_SCORING.md) |
| Reproduce a published score | [Score verification](docs/VERIFYING_SCORES.md) |
| Review the validator | [Validator guide](docs/VALIDATOR_QUICKSTART.md) |
| Find miner/validator commands | [Command guide](docs/COMMANDS.md) |
| Review optional miner reports | [Miner reports](docs/REPORTS.md) |
| Run repository checks | [Testing](docs/TESTING.md) |
| Trace the implementation | [Architecture and review map](docs/REVIEW.md) |

New to Bittensor? The [glossary](docs/GLOSSARY.md) explains the keys, stake,
weights, and Noah's two validator roles.

## Miner participation

See [ordinary scoring](docs/ORDINARY_SCORING.md) for
V2.1 rules and examples. A new eligible miner begins with 0.2 ordinary points,
without an upfront purchase, entry deposit, starter pool or five-NOAH allowance.
Actual earnings are subject to a 50% contribution obligation the following UTC day.
From November 24, 2026 at 00:00 UTC, SN82 NOAH deposited into the Ark earns
zero miner credit. Qualifying foreign subnet alpha and root TAO can satisfy
the obligation. Existing prepaid balances carry forward. There is no miner daemon or axon to run.

Mainnet enabled V2, the optional [daily reporting bonus](docs/REPORTS.md),
weekly assignments and temporary miner burn on November 20, 2026. V2.1
keeps reporting and burn in effect. In bonus mode,
accurate, timely backing reports can add up to 20 points, adjusted for V2 maturity
and debt. Missing a report leaves ordinary scoring intact. Reporting uses the
miner report CLI and is separate from making deposits; it does not require an
axon. The temporary Mainnet burn targets approximately 98% of the miner
allocation after bonuses, so bonus points affect a miner's share of the remaining
allocation. They are not a guaranteed token payment. Legacy scoring, reports OFF
and burn OFF remain the shipping defaults; activation needs separate approval.

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

The [ordinary scoring guide](docs/ORDINARY_SCORING.md) identifies the V2 and V2.1 policies.
Historical modes remain available for replay; verify the mode in live calculation evidence.
Public role addresses are in the [address reference](docs/reference/WALLET_ADDRESSES.md).
Check current chain state and announcements before using either for a live
transaction. Code and tests alone do not establish an operating validator.

For optional local lifecycle diagnostics, see [application events](docs/APPLICATION_EVENTS.md).
Event capture is best-effort and is separate from chain verification.

The source is distributed under the [MIT License](LICENSE). See
[Security](SECURITY.md) for private vulnerability reporting and key handling,
and [Contributing](CONTRIBUTING.md) to propose a change.
