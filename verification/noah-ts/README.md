# Independent TypeScript scoring checks

This directory holds the TypeScript scoring implementation used by Noah's
[twin proof](../../scripts/twin-proof.sh). It reads the
[shared fixtures](../../fixtures/shared/) and produces score output that is
compared byte for byte with the Python implementation. The TypeScript code
does not import the Python scorer.

Use Node.js 24 or newer. From this directory:

```sh
npm ci
npm test
npm run typecheck
```

From the Noah source root, run the complete checks, including the twin proof,
with `./scripts/run-tests.sh`. Install dependencies with `./scripts/install.sh`.

The current numeric settings come from the selected profile in
`docs/parameters/profiles/`; see the [scoring guide](../../docs/SCORING.md).
This README is not a complete scoring specification.
