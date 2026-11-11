# Architecture and review map

This repository contains the scoring calculation, the chain records that feed
it, the validator that publishes weights, and tools for miner contributions.
For the V2 candidate, start with [participation](PARTICIPATION.md),
[ordinary scoring](ORDINARY_SCORING.md) and [score verification](VERIFYING_SCORES.md),
then trace a contribution through the implementation.

This map describes the V2 candidate. Historical starter allocation and
commit/reveal procedures apply only to the versions that used them.

## From a chain receipt to a reward weight

```text
Two independent chain sources
  -> finalized earnings, stake holdings, and contribution receipts
  -> miner attribution and durable history
  -> verified miner identity, preserved accounting, and V2 scoring inputs
  -> Python score + independent TypeScript check
  -> submitted weights with seat-replacement protection
  -> proxy execution, direct weight publication, and finalized chain verification
```

| Boundary | Main implementation | What to check |
|---|---|---|
| Chain reads | `noah/chain/live.py`, `noah/chain/agreeing.py`, `noah/chain/runner.py` | Sources agree; missing reads do not become valid zeros; deliberate fresh checks remain |
| Contribution attribution | `noah/chain/deposits.py`, `noah/deposits/owner_entitlement.py` | Correct Ark, Ark Validator, owning coldkey, and miner hotkey; no shared-wallet double credit |
| History | `noah/chain/store.py`, `noah/chain/fixture_build.py` | Complete periods, holdings and collateral records; safe repeated scans |
| Onboarding and preserved entry accounting | `noah/points/onboarding.py`, `noah/chain/entry_history.py`, `noah/core/chain_adapter.py` | No new entry payment or starter pool; verified ownership and registration; historical entry credit is preserved |
| Receipt valuation | `noah/points/runtime.py`, `noah/points/ema_reference.py`, `noah/points/pricing.py`, `noah/points/receipts.py` | Exact receipt identity and parent-block EMA/spot evidence; unsupported receipts remain visible; missing required evidence is not guessed |
| Calculation | `noah/points/engine.py`, `noah/points/ledger.py`, `noah/points/onboarding.py` | V2 onboarding and scoring match the published rules; historical entry credit is preserved; returned collateral is not earnings; prepaid coverage is not spent twice |
| Independent calculation | `verification/noah-ts/`, `fixtures/shared/` | Shared examples produce matching answers without changing expected results to hide failures |
| Submission selection and evidence | `noah/core/period.py`, `noah/core/chain_adapter.py`, `noah/core/submitted_record.py`, `noah/points/mode.py` | Eligible newcomers participate in ordinary weight allocation; owner-seat exclusions apply; a replacement seat cannot inherit another miner's score; mode and policy match the retained calculation |
| Signing | `noah/core/proxy_route.py`, `noah/core/submitter.py` | Expected signer and grant; inner proxy result checked; ambiguous outcomes stop publication |
| Publication and refresh | `noah/core/loop.py`, `noah/core/chain_adapter.py`, `noah/points/publication.py`, `noah/points/outage.py` | Publish the latest complete day; refresh the verified vector within its allowed lifetime; verify finalized inclusion and applied weights |
| Recovery | `noah/core/commit_ledger.py`, `noah/core/publish_intent.py`, `noah/points/catchup.py`, `scripts/publication_hold.py` | Restart cannot blindly repeat an uncertain send; missed days retain verified accounting without claiming a publication |
| Miner sends | `noah/deposits/assistant/` | Preview, explicit confirmation, destination and delegation checks, receipt read-back |

## Review the difficult cases

Follow an eligible newcomer through registration, its initial V2 score, first
earnings, contribution, calculated score, submitted weight and finalized chain result.
Include a shared coldkey, wrong delegation, an unnamed transfer, locked and
released collateral, advance coverage, catch-up contributions, and a replaced
seat. Separately verify preservation of historical entry credit. Use the existing fixtures and tests before adding a new scenario.

Newly scanned unnamed deposits receive no credit. Older stored rows have a
compatibility path, so changing the reader does not rewrite historical records.
The helper's `credited` status compares supplied totals; it is not proof that
a particular receipt earned credit.

The chain's NonFungible proxy permission is broader than the publication calls
the application constructs. Review both the application checks and the
consequences of a stolen operational key. The hardware-held validator key and
funds-controlling coldkeys must stay off the server.

A matching weight vector may belong to an older publication. For V2 direct
publication, check the transaction, proxy result, submitted vector, finalized
events and block references rather than treating a matching vector alone as
proof of a new publication.

## Reproduce and report

[Testing](TESTING.md) gives the commands and expected outcomes.
[Verification](VERIFYING_SCORES.md) distinguishes reproducible arithmetic from
validated chain inputs. Passing unit tests does not certify an operational
deployment.

For a finding, include the commit, file and line, a reproducer, observed and
expected behavior, and its consequence. Keep secrets and sensitive exploit
details out of public reports; follow [Security](../SECURITY.md).
