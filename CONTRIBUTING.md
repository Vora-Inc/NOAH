# Contributing

Keep a proposed change focused on one
problem and explain what a miner, validator, or reviewer should see afterward.

## Setup

Follow the [validator guide](docs/VALIDATOR_QUICKSTART.md#install-for-review),
then run:

```bash
./scripts/run-tests.sh
```

Include the commands you ran, their results, and any skipped checks with a
proposed change. Do not present local tests as proof of mainnet behavior.

## Scoring changes

Explain the rule being changed and provide a small example with expected
numbers. Changes to scoring must keep the Python and TypeScript outputs in
agreement on shared fixtures. Add a fixture for a new scenario and update its
checksum in `fixtures/shared/MANIFEST.sha256`.

Do not change expected outputs solely to make a failing test pass.
First establish which behavior is correct.

## Chain and contribution changes

Distinguish a missing record from a confirmed zero. Preserve transaction and
block identities when attributing contributions. A change to receipt handling
should cover the wrong destination, wrong validator, ambiguous attribution,
and repeated reads as well as a valid receipt.

Never commit recovery phrases, private keys, credentials, filled environment
files, wallet directories, or production databases. Report security findings
through the process in [SECURITY.md](SECURITY.md).

For setup details and test scope, see [Testing](docs/TESTING.md).
