# Verify ordinary points

A preview, a published transaction and an actual reward are three separate facts.
For the candidate `points-204040-v5`, verify all three without relying on a dashboard.

1. Obtain the retained daily calculation, complete input fixture, pricing and
   migration policy, finalized receipt evidence, agreed output and publication receipt.
2. Confirm chain identity, cutoff, roster identities and rule version. V5 uses
   fixture/output schema `/3`, explicit `pointsRules` and `pointsExcludedUids`, and
   policy `noah-points-204040-90d-no-pool/5`. All rules must match the candidate.
3. Reproduce the arithmetic offline with both implementations:

```sh
.venv-validator/bin/python -m noah.points.run fixture.json independent-output.json
```

The output path must not exist. Agreement proves arithmetic against those inputs,
not their on-chain truth. Compare the complete output to the retained agreed output.

4. Verify input receipts, attribution, historical consumed receipts, policy hashes,
   migration balances and foreign parent-block EMA/spot proofs against independent
   finalized chain records. Do not reprice historical credits or count the balance
   bridge as a payment. The immutable calculation binds these inputs and its vector.
5. Verify the exact submitted vector, signer, finalized transaction and events.
   Then inspect the applicable epoch's consensus and actual mining rewards. A daily
   publication and later republications are distinct receipts. An unknown send
   must be reconciled, never assumed failed or repeated blindly.

For missed publications, distinguish an **accounted, not published** record from
a submitted receipt. Verify its retained complete-day calculation and the
original dates of earnings and deposits. The next complete calculation must
include those days' accounting; missing history must refuse. Only an actual
finalized publication establishes that a score vector was applied on-chain.

See [the rules and examples](ORDINARY_SCORING.md). Older evidence formats remain
supported for historical verification, documented in [legacy verification](LEGACY_VERIFYING_SCORES.md).
No starter allocation should be added to a V5 result.
