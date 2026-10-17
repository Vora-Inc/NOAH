# Test and reproduce the scoring code

These local checks need no funded wallet or production settings. They exercise
saved fixtures and simulated chain behavior; they do not submit a transaction
or prove an installed mainnet validator is publishing weights.

## Install

From the repository root, use a Unix-like environment with Bash, Git,
Python 3.10 or newer, Node.js 24 or newer, and npm:

```sh
./scripts/install.sh
```

The installer creates `.venv` for computation and `.venv-validator` for
checks that require Bittensor, then installs the TypeScript dependencies.
It may install local hooks when the public checkout includes hook settings.
Keep the environments separate.

Both Python environments use the reviewed versions in `constraints.txt`.
The build backend is pinned separately in `pyproject.toml`; Node uses
`verification/noah-ts/package-lock.json`. Constraints pin package versions,
not artifact hashes. Use a trusted package index and retain the installed
package inventory with each deployment.

Review dependency updates together with these files, run the suite on the
supported deployment platform, and check current security advisories before
changing the constraints. The dependency-audit workflow checks the pinned
Python and Node versions on pull requests and weekly. A clean advisory check
does not replace the runtime tests.

## Run the suite

```sh
./scripts/run-tests.sh
```

This runs Python checks in both environments, TypeScript tests and types, and
compares the Python and TypeScript outputs on the shared fixtures. Success
ends with `All green.` Review the summary for failures and skips. A skipped
check is not evidence that the corresponding live behavior passed.

To compare the two scoring implementations again by itself:

```sh
./scripts/twin-proof.sh
```

## Check one saved score

```sh
.venv/bin/python -m noah.scoring.audit_published \
  fixtures/shared/steady-at-target.json \
  fixtures/shared/out-python/steady-at-target.score.json
```

`AGREES` with exit code 0 means this code calculates the saved answer from
the supplied inputs. It does not verify those inputs against the chain.
See [score verification](VERIFYING_SCORES.md) for the additional checks.

## Record the version

When reporting a result, include the commit and dependency versions:

```sh
git rev-parse HEAD
.venv/bin/python --version
.venv-validator/bin/python -c 'import bittensor; print(bittensor.__version__)'
node --version
```

Dependency ranges can resolve differently later. Local success does not
establish complete production history, a live contribution, sealed
publication and reveal, or recovery on a server. Verify those against the
deployed service and finalized chain records.
