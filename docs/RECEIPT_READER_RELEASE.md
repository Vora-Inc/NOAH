# Receipt reader update

Successful `Multisig.as_multi` calls wrapping a flat `Utility.batch_all` of
stake transfers now retain a separate receipt for each proved arrival. Each
receipt keeps its original event ordinal, giver, arriving alpha amount, token,
and destination position.

The reader binds each call to its `ItemCompleted` segment, the executed inner
call hash, and matching removal, arrival and transfer events. Incomplete,
conflicting or ambiguous evidence refuses ingestion. A narrowly validated
leading `BasketDeposited` accounting event supplies no receipt amount.

Named transfers retain their chain-stated origin hotkey. Plain transfers remain
unattributed, and receipts on a different destination validator remain
ineligible. Entry and [scoring rules](SCORING.md) are unchanged.

Both multisig receipt paths accept the SDK's equivalent unit-success forms:
an exact `Ok` result containing null, an empty list, or an empty tuple. Errors,
extra keys and nonempty values remain rejected. This preserves success when
live SDK values differ from their JSON representation.

## Reproduce the public regression checks

[The included batch tests](../noah/chain/tests/test_public_batch_receipts.py)
construct synthetic decoded calls and events. They cover both the alternating
transfer/remark path and transfer-only multisig batches, including:

- Separate unequal arrival amounts and original event ordinals.
- Named versus unnamed transfers, wrong receiving validator, and historical
  owner failures where owner evidence is required.
- Root and non-root assets, with arriving alpha kept distinct from TAO value.
- Missing or conflicting completion, execution, call-hash and receipt evidence.
- Strict optional `BasketDeposited` accounting and `Ok` null/list/tuple forms.
- Idempotent persistence without marking an incomplete day fully scanned.

From the repository root, after installing the test dependencies:

```sh
.venv/bin/python -m pytest -q noah/chain/tests/test_public_batch_receipts.py
```

These are public-safe synthetic regressions, not captured transactions or proof
of a live execution. Raw operational captures and identities are not included.
The transfer-only reader requires the SDK-decoded inner batch's `call_hash` to
match `MultisigExecuted.call_hash`. Missing or conflicting evidence refuses the
recognized batch rather than silently aggregating it. Synthetic tests exercise
that contract; SDK decoding compatibility needs separate validation for the
supported client/runtime combination. A refusal must be investigated and must
not be bypassed by dropping the block or inventing a hash.

Run `./scripts/run-tests.sh` for the full public suite. Deployment and closed-day
scoring acceptance remain separate operational checks.
