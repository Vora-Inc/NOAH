# Verify a score

You can check the arithmetic without a wallet, chain connection, or account.
Checking the underlying earnings and deposits is a separate step.

## Try the included example

From the repository root, after `./scripts/install.sh`:

```bash
.venv/bin/python -m noah.scoring.audit_published \
  fixtures/shared/steady-at-target.json \
  fixtures/shared/out-python/steady-at-target.score.json
```

The successful result includes:

```text
AGREES: the published figures follow exactly from the published inputs.
```

Compare the Python and TypeScript calculations over all shared examples:

```bash
./scripts/twin-proof.sh
```

The command reports how many fixtures agree byte for byte and exits nonzero
on a mismatch. The [testing guide](TESTING.md) covers the full suite.

## Check a published day

For each published day, obtain the input fixture and score output with their
checksums, network identity, relevant finalized block references,
configuration, and scoring release. These records must not contain private
keys or wallet recovery material.

After obtaining a day's pair, use the same command with the two local files:

```bash
.venv/bin/python -m noah.scoring.audit_published \
  /path/to/day.fixture.json /path/to/day.score.json
```

| Result | Meaning |
|---|---|
| `AGREES` (exit 0) | This version calculates the saved answer from the supplied inputs |
| `DISAGREES` (exit 1) | Recalculation differs; the command identifies the first difference |
| `NO VERDICT` (exit 2) | Missing, malformed, or incompatible inputs prevent a comparison |

Use the scoring version named in the day's record. Keep the original files
when investigating a difference. Agreement alone cannot show that a claimed
earning or deposit happened on the chain.

## Verify the complete download

Keep a daily bundle's files together and run:

```bash
.venv/bin/python scripts/verify_bundle.py /path/to/downloaded-day
```

This checks the file list, checksums, known secret patterns, and score
recalculation. Success ends with `PASS`. Exit 1 means a failed check;
exit 2 means verification could not be completed. A secret-pattern scan cannot
prove that arbitrary text contains no sensitive information.

Read `MANIFEST.json` and `chain-refs.json` too. The bundle states whether its
input was captured at scoring time or reconstructed later, and whether the
original published score was supplied. Do not treat a reconstructed example
as the validator's original record. Check out the code commit named in the
manifest before comparing a real day.

## Compare the two scoring implementations

After the [development install](VALIDATOR_QUICKSTART.md#install-for-review):

```bash
./scripts/twin-proof.sh
```

This generates fresh outputs from Python and TypeScript and compares them.
The single saved-score command above checks one recorded answer against its
input using the version in this checkout.

## Check the source records

For your miner, compare the published data with finalized chain records:

- The miner hotkey and owning coldkey over the period being scored.
- Spendable earnings and later unlocks, with no missing days silently treated as zero.
- Each deposit's transaction and block, alpha amount, receiving coldkey, and destination validator.
- The miner named by the receipt, including any wrapper or remark used for attribution.
- The separate entry contribution and the ongoing contribution total.

A checksum proves that a downloaded file matches the published file. It does
not establish that the contents are true. Historical reads also require a node
that retains the state for the relevant block.

A complete chain-to-score walkthrough uses the actual daily records and a
historical-data source. The bundled examples establish reproducibility of
the calculation only.

## Calculated, submitted, and observed weights

The validator saves three different records:

| Record | Meaning |
|---|---|
| `day-N.json` | Calculated scores |
| `day-N.submitted.json` | Integer weights passed to the submission path |
| `day-N.confirmed.json` | A later comparison with finalized chain weights |

An operator can run the comparison against explicit local records and a
verified endpoint:

```bash
.venv-validator/bin/python scripts/confirm_published.py \
  --day 15 --scores-dir /path/to/scores --endpoint wss://YOUR-VERIFIED-NODE
```

Replace the day, directory and endpoint with the intended environment. This
reads the chain and writes a local confirmation record; it does not sign or
submit a transaction. Exit 0 means `MATCH`, 1 means `MISMATCH`, and 2 means
required inputs or chain data could not be read.

For sealed publication, wait for the chain's reveal and inspect the matching
publication evidence. An identical vector can belong to an older submission,
so `MATCH` alone does not prove that this day's commit revealed. The script
also relies on the operator choosing the correct chain; do not use it to
certify network identity or interrupted-send recovery.

## Check a sealed proxy publication

For sealed publication, this operator check connects the submitted record to proxy execution,
the commit event, the reveal event and the resulting weights. It needs the
operator's score and publication-state directories, not a private key:

```bash
.venv-validator/bin/python scripts/verify_proxy_publication.py \
  --day DAY_NUMBER \
  --validator YOUR_SCORING_IDENTITY \
  --signer YOUR_PUBLICATION_PROXY \
  --scores-dir /path/to/scores \
  --state-dir /path/to/publication-state \
  --endpoint wss://YOUR-VERIFIED-NODE
```

Replace the day, both public addresses, paths and endpoint before running. Exit 0 means all checks
passed; 1 means a failed check; 2 means the tool could not establish a result;
3 means the expected reveal is not due yet. A failure or unreadable record is
not a successful publication.

This tool's tests use a simulated chain. Its runtime names and automatic
reveal-block calculation still need confirmation against the intended live
runtime. `--reveal-block BLOCK_NUMBER` lets an operator supply the known reveal
block. The tool prints the sealed commit hash but does not compare it with
saved sealed bytes, because those bytes are not retained after success. A pass
is evidence of normal publication, not proof of interrupted-send recovery.
It also needs local operator records that are not all part of the public daily
score download. An outside reviewer can reproduce the score independently
without access to the validator server.

For direct publication, the validator retains the finalized transaction, proxy
inner-call success, matching weights event and exact stored vector in the
submitted record. Use the [publication recovery runbook](runbooks/publication-status-unknown.md)
when that outcome is uncertain; a sealed-reveal check does not apply to direct mode.
