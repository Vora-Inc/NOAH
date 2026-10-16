# Optional application event log

The miner assistant and validator can emit local application diagnostics. Export
`NOAH_EVENT_LOG` as an absolute path to a JSONL file before starting the process.
Use an existing directory owned by the operator. The directory path and final
file must not contain symlinks. The writer requires a local POSIX filesystem
supporting file locks and `fsync`; it creates the file with mode `0600`.

For example, with an existing `/var/lib/noah/events` directory:

```sh
export NOAH_EVENT_LOG=/var/lib/noah/events/validator-events.jsonl
```

Omitting the variable disables capture. Configuration is read from the process
environment when the first event is emitted. It stays fixed for that process.
The log does not require a server or change the miner's manual confirmation flow.
It performs no additional chain reads, signatures or transactions.

## Meaning and fields

Records use `schema: noah-runtime-event/1`, UUID `event_id` and `run_id`, a
per-run `source_sequence`, UTC `recorded_at`, `source` (`miner` or `validator`),
`event` and `stage`. Events mark stage entry, normal return or exception, plus
observed registrations and recorded contribution states. Validator stages cover
process execution, startup gates, ticks, ingestion, scoring, agreement and
publication. Miner stages cover preview, submission, reconciliation and ledger
transitions. A returned refusal or waiting result is still a normal stage return;
its allowlisted `outcome` records that distinction when available.

All records have `evidence: application_event` and
`public_finality_verified: false`. Even an application's `published`, `finalized`
or `credited` outcome is its own reported state. It is not independent chain
proof. A successful submission-stage return does not establish a later sealed
reveal. Consumers must not use this stream to authorize a retry or payment.

Optional numeric fields include network, day, height, period, seat and receipt
indices and amount. They are omitted unless plain nonnegative integers within
JavaScript's exact integer range. `genesis_hash` appears only after the calling
invocation has observed it; configured network identity is never substituted.
Receipt block/transaction hashes and UUID intent identifiers are included only
where already available. Seat, miner and validator hotkeys are checksum-validated
public SS58 addresses; a seat snapshot does not infer which role a seat holds.
A received token's subnet is reported separately from the configured network.
No exception text, endpoint, wallet path, seed, key material or arbitrary payload
is serialized. Consumers should join by run/sequence, intent or observed chain
identity, never by amount alone.

## Capture limits and operation

The worker queue holds at most 4096 pending events. File writes and `fsync` happen
on a daemon worker, outside scoring/signing/retry decisions. The file is capped
at **16 MiB** and rotates into three retained archives (`.1`, `.2`, `.3`),
for at most 64 MiB of JSONL. A stable `.lock` coordinates rotation. Full queues, lock contention,
unsupported filesystems, unavailable paths, partial log tails and write failures
lose events. Existing complete records survive normal restarts; each new process
uses a new run ID. An interrupted write may leave an incomplete final line; the
writer then refuses further appends until the operator repairs or archives it.

`source_dropped` is a cumulative best-effort loss counter. Later sequence gaps or
an increased counter can reveal missing events. Terminal losses, contention
before sequence allocation and a permanently unwritable file may leave no
later record proving the loss. **A gap-free stream does not prove complete
capture.** Normal process exit attempts a drain for at most 0.2 seconds; abrupt
termination can lose pending events.

The atomic `<event-log>.health.json` snapshot refreshes about every five seconds,
even without a new event. Check its timestamp, status, `source_written`,
`source_dropped`, `losses_detected`, `health_write_failed` and latest writer PID/run.
Missing or stale health is unavailable evidence. A recovered writer can report
`ready` while its cumulative loss counter remains nonzero.

Monitor disk space, health and retention coverage. Preserve an incomplete tail
with the process stopped before operator repair; do not overwrite active logs.
The bounded archives can expire unread events during long collector outages.
Do not assume a collector has safely retained them without checking. Logging
failure remains best-effort and does not halt the miner or validator.
