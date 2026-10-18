# Test a conversion followed by mixed deposits

This command currently supports **private test chains, fixed budgets and direct signing only**.
Production mixed deposits are not supported in this release. Changing `test_only` to false does
not enable production. Fixed direct and proxy deposits are described in the
[quickstart](MINER_QUICKSTART.md) and [proxy guide](MINER_PROXY.md).

## What the command does

1. Calculate the fixed NOAH contribution budget from the deposit settings.
2. Allocate part of that same budget to a protected NOAH-to-foreign-alpha swap.
3. Wait for the swap to finalize and read the actual NOAH spent and foreign
   alpha received from its exact chain events.
4. Deposit the remaining NOAH budget, then the actual foreign output, in separate
   attributed transfers to the configured Ark positions.

The gross NOAH debit includes source alpha paid into the swap plus its alpha fee.
The receipt records those separately; destination TAO fees are never interpreted
as foreign alpha. Existing foreign holdings are excluded. A price quote is an estimate, never a
receipt or a scoring valuation. Each transaction is separate: a failure can leave
a partially completed workflow. An uncertain result is reconciled before another
step; the command never automatically repeats a possibly sent transaction.

## Configure a disposable test

Use the same active INI from the quickstart (`noah-deposit.ini` below). Set its private-chain endpoint and
pinned genesis, disposable owner/miner identities, and disposable Ark recipient.
Use a fixed contribution budget in `[deposit]`, for example:

```ini
[deposit]
mode = fixed
amount_noah = 0.2
reserve_noah = 0
cap_noah = 0.2
```

Add the following section, replacing every placeholder after checking the target
runtime and receiving position:

```ini
[mixed]
test_only = true
foreign_netuid = YOUR_FOREIGN_SUBNET_NUMBER
foreign_ark_hotkey = YOUR_TEST_ARK_RECEIVING_HOTKEY
swap_amount_rao = 100000000
max_slippage_bp = 100
quote_max_age_blocks = 5
max_fee_rao = 10000000
runtime_code_hash = REVIEWED_RUNTIME_CODE_HASH
spec_version = REVIEWED_RUNTIME_SPEC_VERSION
```

One NOAH is 1,000,000,000 rao. This example allocates 0.1 NOAH of the
0.2 NOAH budget to the swap, leaving 0.1 NOAH for the NOAH deposit.
The foreign deposit is determined only after the swap finalizes.

Alternatively, replace `swap_amount_rao` with `swap_percent_of_budget_bp`:
2500 means 25% **of the contribution budget**, rounded down. Set exactly one
sizing field. This is not a percentage of total wallet holdings or total emissions.
The allocation must leave a positive NOAH deposit. Fee limits are in TAO rao;
the command checks estimates with twice their amount as headroom.

## Preview, send and inspect

Activate the installed environment, then inspect the settings:

```sh
noah-miner --config noah-deposit.ini config
```

Preview without loading a signing key or submitting:

```sh
noah-miner --config noah-deposit.ini convert-deposit --preview --label mixed-test-01
```

Review the budget, receiving positions, estimated output and fees. Then send:

```sh
noah-miner --config noah-deposit.ini convert-deposit --label mixed-test-01
```

Inspect the same operation without sending remaining steps:

```sh
noah-miner --config noah-deposit.ini convert-deposit --status --label mixed-test-01
```

Reuse the same configuration and label when continuing the same operation.
A changed configuration cannot silently redefine an existing label. An incomplete
operation blocks a new label for the same owner in the shared journal. Keep the
journal and canonical deposit state durable; all commands and workers spending
the same owner must share both. The direct and mixed commands acquire the
canonical ledger lock before the mixed journal lock. An unresolved direct/proxy
intent blocks mixed execution, and an incomplete mixed reservation blocks direct
execution, even if another mixed journal path is selected. Missing reservation
state requires investigation; changing a label or path cannot bypass it. The default is
`mixed/` under the configured state directory; `--journal /ABSOLUTE/PATH` selects
an explicit shared location.

`delivery_complete` means all three finalized transaction results were verified.
`credit_verified` and `settlement_verified` remain false: the miner command does
not prove validator credit, staking-share settlement, or reward publication.

Percentage/maintenance contribution modes are currently refused before any chain
or signing access. Shared earnings-period coverage accounting is still required;
the arithmetic helper alone does not implement recurring contributions. A new
fixed label deliberately requests a new contribution.

## Minimum amounts and a confirmed early failure

Preview checks the runtime's minimum using simulated TAO output, not the raw
NOAH amount. For runtime v478 the output must be strictly greater than
`InitialMinStake`; equality is refused. Prices can change, so the minimum is
checked again before signing and submission. The example amount is not a
guarantee that another chain's minimum or available balance will permit it.

A finalized `AmountTooLow` conversion with no stake movements and no downstream
deposit steps may be explicitly closed after the command rechecks its exact
transaction, runtime, failure and fee:

```sh
noah-miner --config noah-deposit.ini convert-deposit --close-failed --label mixed-test-01
```

This asks for `CLOSE FAILED mixed-test-01`. It preserves the original intent,
receipt and fee, and permanently prevents that label from sending. It releases
the pending-operation hold; it does not retry, mark delivery complete, or send a
replacement. Uncertain results, other dispatch errors, and any partially
successful workflow remain held. A separately reviewed configuration and new
label are required for a new attempt.
