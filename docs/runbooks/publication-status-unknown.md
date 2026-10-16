# When a weights submission has an unknown outcome

This operator procedure accompanies the release candidate's publication
safeguards. It is not a miner deposit-recovery procedure. For deposits, use
the [helper reference](../../noah/deposits/assistant/USAGE.md).

`PUBLICATION STATUS UNKNOWN` means the validator began a direct or sealed weights
submission and cannot establish its outcome. It stops further submissions.
In sealed mode the chain reveals an accepted commit later; the operator does not
manually send a separate reveal.

## Keep publication stopped

Stop the publisher using the approved host procedure. Confirm it is stopped
and preserve its state before investigating. Any manual recovery must use
the same service account, environment, state directory and host publisher
lock as the service. An idle loop lock alone does not establish that the
service is stopped.

Keep `loop.json`, `commit-ledger.json`, `publish-intent.json`,
`publish-backoff.json`, the calculated/submitted/confirmed score files, any
`publish-hold.json`, logs and the history database. Do not delete progress or
switch to direct weights to get past a refusal.

## Establish what happened

The intent marker records the day, subnet, signer and available send details.
Newer markers can include the call, sealed-commit digest, reveal round,
send time, observed nonce and head. They may not contain an extrinsic hash.
Record missing information as missing.

Use a finalized chain source to inspect the entire relevant block range from
the recorded send head to the current head. If that head was not recorded,
establish a range covering the attempted send from the preserved evidence.
An unreadable portion makes the result incomplete.

| Finding | Action |
|---|---|
| The matching commit is found | Preserve the block, signer, subnet and commit evidence. Confirm the validator can attribute it to the intended day before resuming. |
| A commit is found outside the automatic recovery scan window | Keep publication stopped. Reconcile the day-to-commit record through the approved recovery procedure; do not clear the marker and resend. |
| No matching commit is found | This alone is insufficient. Establish that the earlier transaction cannot still land, as described below. |
| Records are missing or contradictory | Keep publication stopped and report the unresolved transaction details. |

For an attempt proven to have failed before signing and broadcast, preserve the
exact error, installed SDK/source identity and reproduction of the failing stage.
Confirm the deployed SDK's execution order places that stage before signing and
submission; a generic timeout or an error's wording alone is insufficient.
Corroborate with a finalized chain scan and archive the original intent with the
reconciliation receipt before removing it from the active path. Keep the publisher
stopped and held throughout reconciliation and installation.

If signing/broadcast may have occurred, there are two possible ways to establish
that an unfound transaction cannot still land. Either requires a complete chain
scan taken after the evidence:

- Decode the **actual signed transaction's** validity window and show that it
  has expired. The marker's `era_period` is an assumed library default, not
  proof of the signed transaction's lifetime.
- Establish that its nonce has been consumed. The observed account nonce at
  send time is sufficient only if there were no other pending transactions
  or other signer activity that could make it differ from this send's nonce.
  If that cannot be established, this route is unavailable.

Elapsed time alone never clears the stop. Neither does a guessed transaction
lifetime. After reconciliation, preserve the marker as evidence rather than
deleting it. The approved host procedure must name who records the outcome
and authorizes resumption.

## Restoring a backup

A backup cannot contain a submission made after it was taken. Establish a
publication hold **before starting** a restored validator, even if the backup
has no open intent marker. Keep the old publisher stopped so there is only
one possible signer process.

The candidate includes `scripts/publication_hold.py` with `set`, `status`
and `release` commands. Run it with the production service's actual environment
and state location. Running it from an unrelated shell can inspect or change
a different directory. A `status` result of `no hold` is not proof that the
chain is reconciled.

Reconcile every possible publication since the backup before releasing the
hold. Record the checked ranges, receipts and unresolved items. Release
requires an evidence note; writing a note does not itself prove recovery.

After the approved restart, check the intended day's score file, accepted
chain submission and finalized read-back. Confirm no duplicate publication.

## Manual recovery of a direct publication

Pin the original scoring day/height and preserve its fixture, score, vector and
calculation manifest. Recompute and compare before sending; explain any change
in inputs, policy or seat ownership rather than silently accepting it. The manual
pipeline retains the same linked calculation evidence as the automatic path
before publication. Evidence persistence failure must prevent sending.

Use the installed target-aware manual-publish wrapper with the normal service
stopped, its real environment, and both publisher/loop locks. Release the hold
only after reconciliation; releasing it does not itself start the service. Verify
the exact finalized extrinsic, proxy inner success, scoped WeightsSet event and
complete runtime-normalized stored vector. Then verify the submitted record and
calculation links, and only afterward restore the normal service.

A finalized send followed by a lost response or failed local record write is
reconciled without resending. On-chain state and local files are not one atomic
transaction. An included proxy inner failure may spend a fee without applying
weights. An unresolved result remains held, never assumed unsent. While a day is
missing, prior weights remain effective; recovery does not reverse emissions
already distributed under those weights.

## Proxy route

There are two public identities. `NOAH_EXPECTED_SIGNER_ADDRESS` is the Noah
validator: its seat, weights, commit event and reveal remain under that address.
`NOAH_PROXY_SIGNER_ADDRESS` is the operational signer: its nonce and account
pay any transaction fees. Reconcile the proxy account's nonce, not the
hardware-held validator's nonce, when deciding whether an earlier signed
transaction can still land.

For an included transaction, inspect its own `ProxyExecuted` result and matching
validator commit event. An outer success with an inner error did not publish
weights. An absent or unreadable inner result is unresolved, not success.

Three consecutive failed sends stop further publication. The failure record
also spaces retries. With the service's real environment loaded, inspect it:

```bash
.venv-validator/bin/python scripts/publication_backoff.py status
```

Exit 0 means no failure record; exit 3 means a record exists. Neither clears
an unresolved submission or proves the chain state. After the failure is fixed
and publication is reconciled, an authorized operator can archive and reset
the failure record with `reset --evidence-note "..."`. Resetting the failure
counter does not clear the separate publication hold or unknown-send marker.

The backup must retain the public validator/proxy identities and grant facts,
but exclude wallet keys and recovery words. Read the grant again on the
selected chain; a completed read does not by itself prove that the grant is
present or usable.

A lost operational key can be restored through the approved offline custody
procedure, or replaced with a new key and a new hardware-authorized grant.
A suspected compromise requires revoking the old grant and reconciling every
possible publication before resumption. The hardware-held validator identity
and owner multisig need not be exported to perform recovery.
