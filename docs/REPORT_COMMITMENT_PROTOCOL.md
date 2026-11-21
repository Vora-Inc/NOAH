# Report commitment formats

`context-v1` is an opt-in protocol for independently selecting multiple reports
from the same miner in the same time windows. It does not activate reporting,
change scoring, publish weights, or authorize Mainnet transactions. A trusted
report-only round selects it with `"commitment_format": "context-v1"`. The
schedule and weekly template must carry that setting into each complete round.
Without that field, historical Raw32 SHA-256 commitments and their original
single-commit selection rules remain unchanged. There is no wire auto-detection
or fallback from an explicitly selected format to another format.

## Exact encoding

The new commitment is one 79-byte `BigRaw` value in
`Commitments.set_commitment`, targeting the mining subnet:

| Offset | Bytes | Value |
| --- | --- | --- |
| 0 | 11 | ASCII `noah-rpt-c1` |
| 11 | 2 | Mining subnet, unsigned 16-bit little-endian |
| 13 | 2 | Reported subnet, unsigned 16-bit little-endian |
| 15 | 32 | Round ID |
| 47 | 32 | Commitment digest |

Round ID is SHA-256 of the **entire validated round**, including
`commitment_format`, encoded as UTF-8 JSON with sorted keys, separators `,` and
`:`, and no NaN values. It binds genesis, both subnets, snapshot, all time
windows, date, eligibility/calculation versions, release and Ark identities.
No caller supplies a separately trusted round ID.

The commitment digest is SHA-256 of the first 47 envelope bytes concatenated
with the **entire unchanged report preimage**. The report-only `noah-rpt-v1`
preimage is exactly **203 bytes**, as defined by `REPORT_FORMAT`; the historical
deposit-policy preimage is 207 bytes. No bytes are truncated. The reveal remains
one 203-byte `BigRaw` value. Domain, explicit subnet IDs and round ID are inside
the hash input, so relabeling an existing digest cannot preserve its reveal.
The envelope and reveal each fit within the existing 256-byte BigRaw limit.

## Selection and verification

For each miner hotkey, mining subnet, reported subnet and round ID, the first
successful, finalized, structurally valid matching envelope **inside the commit
window** stands. Order is block number, then extrinsic index. Structural validity
does not assert answer accuracy: a valid envelope with an incorrect answer or
without a reveal still occupies that context. Later commitments cannot replace
it. Envelopes for another context cannot occupy this one.

The earliest successful matching reveal inside the reveal window is selected
by its domain-bound hash. The verifier then independently checks envelope
context, payload identity and integer calculation outputs. It does not hunt
through later commitments for an answer that passes. If the selected commitment
has no reveal, only that report is `MISSING reveal`; other assigned reports can
remain verified. An entirely absent target is `MISSING commit`, not a malformed
report attributed from another target. If no timely commitment exists, the first
observed matching-context envelope can be retained solely for early/late
diagnostics; it cannot earn a verified result. Out-of-window reveals likewise
remain diagnostic and never replace a timely matching reveal.

Historical rounds without `commitment_format` use the original pre-weekly
selection behavior, including their original diagnostic rules. Their fixture,
preimage, commitment and sidecar bytes are not migrated or reinterpreted.

## Durable miner state and APIs

`preparation_from_round` passes `context_round=<full validated round>` to
`prepare` only for an explicit context round. The prepared file uses schema
`noah-report-prepare/2`, retaining `context_round`, `commitment_envelope_hex`,
`commitment_sha256` (the domain-bound digest), and the unchanged reveal bytes.
`Prepared.wire_commitment` exposes the envelope; existing `Prepared.commitment`
remains a 32-byte digest for journal identity. Historical sidecar schemas and
constructor fields remain supported.

Loading verifies the complete envelope against the retained round and preimage.
Submission also compares that retained round to the trusted requested round
before opening a wallet and again before signing. A context round cannot submit
a historical sidecar or a sidecar valid for another round.

Existing exclusive journals and signer serialization still apply. An uncertain
send is reconciled by its recorded transaction hash, never resent; finalized
commitments permit subsequent target commits to overwrite the single current
`CommitmentOf` slot. Historical block calls, not that mutable slot, provide
per-target collector evidence. Immediate commit receipt acceptance checks the
exact 79-byte envelope; it does not confuse the envelope with its inner digest.

## Qualification boundary

Offline tests cover every ordering of three commits and reveals, missing first
and third reveals, an entirely unsent target, duplicate same-context commits,
wrong answers, invalid candidates, relabeling, historical compatibility,
collector decoding, durable preparation and uncertain-send recovery. Target-chain
BigRaw encoding, commitment rate limits, fees, finality timing and the complete
miner/validator workflow still require the separately authorized Testnet rehearsal.
No source test authorizes activation or establishes deployed behavior.

## Finalized quota preflight

The Python read-only entry point is
`SubstrateChain.commitment_quota(mining_netuid, public_hotkey, required_space)`;
`read_commitment_quota(substrate, mining_netuid=..., hotkey=...,
required_space=...)` also accepts an existing SDK connection. A trial can request
909 units for the whole three-report batch. This reads public state only, without
loading a wallet, requesting a nonce, creating a journal or sending a transaction.

The `noah.commitment-quota/1` receipt records genesis, finalized block/hash/time,
observed runtime spec/transaction/code identity and reviewed source pin, mining
subnet, public hotkey, subnet epoch counter, stored usage (or an absent Option),
effective usage, capacity, required space, saturating total and `allowed`.
Runtime qualification preserves the existing adapter policy: runtime483 requires
its exact code hash; runtime490 uses the supported-spec policy and records its
observed code without claiming an additional code-hash allowlist.

All state queries use the same finalized hash. The reset test is exactly
`stored.last_epoch != SubnetEpochIndex[mining_netuid]`; equality retains the
stored usage. An absent `UsedSpaceOf` defaults to zero, but a query error,
unknown response shape, boolean or out-of-range integer refuses. Addition
saturates at u64 maximum and the total must be **less than or equal to** the
on-chain u32 `MaxSpace` value. `get_epoch_index` ignores the block-number argument
at both reviewed pins: see [runtime483 weights.rs](https://github.com/RaoFoundation/subtensor/blob/db3fdeb98bc82c38b238bfac9f6a3303a3361ee4/pallets/subtensor/src/subnets/weights.rs#L1459)
and [runtime490 weights.rs](https://github.com/RaoFoundation/subtensor/blob/d967d13588834f4f5aaa8ef4c8fc1a865d581462/pallets/subtensor/src/subnets/weights.rs#L1459).
No tempo arithmetic, look-ahead or predicted reset time is used.

Before each new native-chain phase, the miner checks the actual payload charge
under its existing sender lock, before wallet access or phase journal creation.
Insufficient capacity returns `waiting`, `sent: false`, reason
`commitment_quota`, and the receipt. The next tick rereads state. The original
phase windows stay fixed. After the pinned quota reads finish, a fresh finalized
clock check runs before wallet access; the original receipt remains unchanged.
After wallet unlock, a new phase checks the clock again before journal creation.
A deadline crossed by either operation prevents a new transaction. Existing
journals bypass new quota checks
and reconcile their original attempt; unknown outcomes never become resends.
Successful send results also include the preceding quota receipt.

A positive preflight does not reserve quota atomically. Other activity by the
same hotkey or a capacity change before inclusion can still cause failure.
Finalized failures and uncertain sends retain the existing terminal or
reconciliation behavior. The helper never clears journals or changes runtime
parameters to obtain capacity.
