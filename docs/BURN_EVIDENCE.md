# Effective Burn verification

Burn allocation remains off by default. Enabling it still requires separately
approved V2 activation, the existing owner destination checks, Yuma consensus
and weight limits. Allocation arithmetic is unchanged.

An explicit raw `RecycleOrBurn=Burn` value follows the existing verification
path. A missing raw value can qualify only through the additional proof below;
an SDK returning its default `Burn` is insufficient. Explicit `Recycle`, failed
reads, malformed responses and unreviewed default semantics refuse publication.

## Runtime-default proof

Only exact reviewed runtime 483, 492 and 493 code/V15-metadata tuples are admitted.
Their official coinbase implementations burn owner-directed miner incentive
when `RecycleOrBurn` is Burn or absent. Their declared storage default is Burn.
The code constants are separate from general runtime compatibility lists.

For a missing raw value the validator chooses a common finalized block and:

1. Checks the owner coldkey, owner hotkey and exactly one destination seat.
2. Reads runtime version, deployed code hash and explicit V15 metadata from
   each independently configured reader. The full metadata digest authenticates
   the reviewed storage declaration, Identity u16 key, enum and default bytes.
3. Reads the actual storage at that hash twice, retaining null as null, and
   checks the block identity again. The author RPC must return the same complete
   evidence as the paired readers, including genesis, subnet, block, key,
   runtime, metadata, raw value and effective origin.
4. Uses effective Burn only after these checks, with fresh checks again before
   sending. A previous calculation's proof never authorizes a later send.

These are independently checked RPC observations, not a verified Merkle storage
proof. The default proof and owner reads share a block. Existing weight-limit
and Yuma checks remain separate author reads. Finalized checks cannot guarantee
that configuration or runtime behavior will remain unchanged through inclusion
or later emission execution.

No state or metadata proof is cached across checks. The helper uses the explicit
target-state V15 API rather than the SDK parent-runtime codec or spec cache.
This also avoids confusing a legacy V14 digest with the required V15 identity.

## Retained records and compatibility

The existing `noah-burn-allocation/1` proof optionally includes `burn_context`
with its own `noah-effective-burn/1` schema. It records calculation-time evidence,
not the final pre-sign observation. Fresh send evidence is checked independently;
a later valid finalized block does not invalidate the original calculation.
The evidence is outside `limits`, whose allocation inputs remain unchanged.

Existing explicit-Burn receipts remain readable and are not rewritten. New
contexts are checked for exact schema, reviewed identity, raw/origin consistency
and binding to the calculation's genesis/subnet. Same-day burn removal preserves
the original receipt and can recover without requiring current Burn semantics.

This verification support does not activate a feature or prove a deployed
release. Every changed release needs its own artifact checks and Testnet
qualification; Mainnet activation remains a separate human decision.
