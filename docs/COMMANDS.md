# Miner and validator commands

Use `--help` after any command. Existing Python module entrypoints remain valid.
Configuration stays in the existing miner INI and validator environment files.

| Task | Command | Effect |
| --- | --- | --- |
| Inspect miner configuration | `noah-miner config` | Read settings |
| Preview contribution | `noah-miner deposit --preview` | Read/calculate only |
| Make contribution | `noah-miner deposit` | May sign and send after existing confirmation |
| Inspect miner state | `noah-miner status` | Read/reconcile existing workflow |
| Test-only mixed-asset contribution | `noah-miner convert-deposit --help` | Existing workflow and safety checks |
| Test-only purchase and contribution | `noah-miner purchase-deposit --help` | Existing workflow and safety checks |
| Prepare/report/check a round | `noah-miner report --help` | Separate report commands; submit may send |
| Advance daily reporting | `noah-miner report run --schedule report-schedule.json` | Uses a frozen schedule; submission requires existing confirmation |
| Report the weekly assignment | `noah-miner report assignments --yes --follow` | Uses the configured URL and independently trusted assignment public key to complete today's reports. Does nothing without a URL or explicit file |
| Check the report endpoint | `noah-miner report preflight` | Read-only. Checks the configured endpoint, runtime, and Sunday roster block when the week file is available. Does not unlock a wallet or send |
| Validator startup checks | `noah-validator check --env validator.env` | Existing dry run, no publication |
| Continuous validator | `noah-validator run --env validator.env` | Existing validator, may publish |
| Starter expiry guard | `noah-validator guard --env validator.env` | Existing guard, may publish fallback |
| Read saved validator state | `noah-validator status --state loop.json` | Dated file contents, no live health claim |
| Twin-scored fixture preview | `noah-validator score --fixture fixture.json` | Offline; never publishes |
| Collect/verify reports | `noah-validator reports --help` | Read-only chain collection and offline verification |
| Resolve a scheduled reporting day | `noah-validator reports resolve-day --help` | Reads paired finalized evidence; does not enable rewards |
| Write the weekly assignment file | `noah-validator reports publish-week --help` | Supervised write of one Sunday. The running validator writes the current week itself when `NOAH_WEEKLY_ASSIGNMENTS=true` |
| Create a verified public report file | `noah-validator reports export-public --help` | Replays retained evidence and writes local output; does not publish it |

Report processing, V2 scoring and burn are independent. Shipping defaults are
`NOAH_REPORT_GATE_ENABLED=false`, `NOAH_WEEKLY_ASSIGNMENTS=false`,
`NOAH_SCORING_MODE=legacy` and `NOAH_BURN_ALLOCATION_BPS=0`. Mainnet enabled V2 on November 20, 2026, explicitly selected
`NOAH_REPORT_REWARD=bonus-20` with reports ON, and enabled temporary `9800` burn. These report and burn
settings continue with V2.1 from November 24, 2026 at 00:00 UTC.
Nonzero burn requires ordinary V2 or V2.1 scoring; separate settings do not imply every combination is supported.
In bonus mode, missing a report leaves ordinary V2 scoring intact; accurate
reporting can add up to 20 points subject to maturity and debt. The default reward
selector `gate` retains the older penalty behavior and differs from the live Mainnet bonus mode.
See [Reports](REPORTS.md) for policy, reward and burn limits.

The validator supports direct report-aware publication with Legacy, V2 or V2.1.
Startup requires a valid frozen report policy, a matching mining subnet and an
explicitly selected
isolated report worker loading this exact source. Network and retained activation
cutoff checks run before wallet loading. Recovery and exact-release qualification,
followed by manual activation approval, are still required. Existing penalty-mode
state needs a reviewed transition before bonus mode; offline previews do not
activate any feature. The penalty-preview flag `--require-accurate-miner-report`
does not preview the new bonus.

The scheduled miner command advances the existing report workflow; it does not
install a service. `--follow` starts no new tick after its timeout; an in-flight
tick is not interrupted. Use a supervised `submit --round` to follow one fixed,
prepared round and retain its journals and receipts. Report submissions retain
the signing confirmation and separate Mainnet opt-in. Resolving a schedule or
exporting a local verified report does not change validator settings or publish
website content. See [Reports](REPORTS.md) for exact commands, policy/evidence
requirements, signing and retry behavior, and the separate offline preview.
