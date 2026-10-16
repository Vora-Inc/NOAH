# Architecture and review map

This repository contains the scoring calculation, the chain records that feed
it, the validator that publishes weights, and tools for miner contributions.
Start with [the participation rules](CONTRIBUTIONS.md) and
[scoring](SCORING.md), then trace a contribution through the implementation.

## From a chain receipt to a reward weight

```text
Two independent chain sources
  -> finalized earnings, stake holdings, and contribution receipts
  -> miner attribution and durable history
  -> entry eligibility and scoring inputs
  -> Python score + independent TypeScript check
  -> submitted weights with seat-replacement protection
  -> proxy execution, sealed commit, reveal, and chain read-back
```

| Boundary | Main implementation | What to check |
|---|---|---|
| Chain reads | `noah/chain/live.py`, `agreeing.py`, `runner.py` | Sources agree; missing reads do not become valid zeros; deliberate fresh checks remain |
| Contribution attribution | `noah/chain/deposits.py`, `noah/deposits/entry_gate.py` | Correct Ark, Ark Validator, owning coldkey, and miner hotkey; no shared-wallet double credit |
| History | `noah/chain/store.py`, `fixture_build.py` | Complete periods, holdings and collateral records; safe repeated scans |
| Calculation | `noah/scoring/`, `noah/standard/` | Entry is separate; returned collateral is not earnings; prepaid coverage is not spent twice |
| Independent calculation | `verification/noah-ts/`, `fixtures/shared/` | Shared examples produce matching answers without changing expected results to hide failures |
| Submission selection | `noah/core/period.py`, `chain_adapter.py`, `submitted_record.py` | Eligible newcomers retain weights; a replacement seat cannot inherit another miner's score |
| Signing | `noah/core/proxy_route.py`, `submitter.py` | Expected signer and grant; inner proxy result checked; ambiguous outcomes stop publication |
| Recovery | `noah/core/commit_ledger.py`, `publish_intent.py`, `scripts/publication_hold.py` | Restart cannot blindly repeat an uncertain send |
| Miner sends | `noah/deposits/assistant/` | Preview, explicit confirmation, destination and delegation checks, receipt read-back |

## Review the difficult cases

Follow the same miner through registration, entry, its first earnings,
contribution, calculated score, submitted weight, and finalized chain result.
Include a shared coldkey, wrong delegation, an unnamed transfer, locked and
released collateral, advance coverage, catch-up contributions, and a replaced
seat. Use the existing fixtures and tests before adding a new scenario.

Newly scanned unnamed deposits receive no credit. Older stored rows have a
compatibility path, so changing the reader does not rewrite historical records.
The helper's `credited` status compares supplied totals; it is not proof that
a particular receipt earned credit.

The chain's NonFungible proxy permission is broader than the publication calls
the application constructs. Review both the application checks and the
consequences of a stolen operational key. The hardware-held validator key and
funds-controlling coldkeys must stay off the server.

A matching weight vector may belong to an older publication. Check the
transaction, proxy result, commit identity, reveal, and block references rather
than treating a matching vector alone as proof of a new publication.

## Reproduce and report

[Testing](TESTING.md) gives the commands and expected outcomes.
[Verification](VERIFYING_SCORES.md) distinguishes reproducible arithmetic from
validated chain inputs. Passing unit tests does not certify an operational
deployment.

For a finding, include the commit, file and line, a reproducer, observed and
expected behavior, and its consequence. Keep secrets and sensitive exploit
details out of public reports; follow [Security](../SECURITY.md).
