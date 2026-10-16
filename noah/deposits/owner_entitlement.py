"""Check whether a depositing coldkey is entitled to credit a miner hotkey.

Historical attribution uses ownership at the specified block hash. Do not replace
that observation with current ownership, a configuration label or a cached answer."""

from ..chain.live import _account_id32_address
from ..chain.sources import Refusal
from ..chain.ss58 import NotAnAddress, public_key_of


class OwnerReadUnreadable(Refusal):
    """The Owner storage read for a hotkey at a specific block was not a
    real, decodable address. Nothing is known rather than nothing found."""


class OwnerNotEntitled(Refusal):
    """The depositing coldkey did not own the named hotkey at the deposit's
    own block, so it had no standing to assign that deposit's credit to it."""


def confirm_owner_entitled_at_block(source, *, hotkey, coldkey, block_hash):
    """Confirm ``coldkey`` owned ``hotkey`` AT THE EXACT BLOCK ``block_hash``
    names -- never at chain head, never "live".

    ``source`` is any object offering ``owner_at(*, hotkey, block_hash)``,
    returning ``Owner[hotkey]``'s raw, undecoded chain value pinned to that
    block hash (the same block-pinning contract
    ``noah.deposits.live_chain_source.LiveChainSource.holding_at`` already
    uses for a stake read, and the same duck-typed, injectable-fake shape
    ``noah.ark.live_source.LiveSource`` uses elsewhere in this package --
    every test in this module's own suite passes a fake, never a live
    connection).

    Raises :class:`OwnerReadUnreadable` if the read itself fails, or if what
    comes back cannot be decoded as a real chain address (the identical
    three-shape ``AccountId32`` decode ``noah.chain.live``'s own
    ``hotkey_owners`` already uses for this exact storage item, reused here
    rather than re-derived). Raises :class:`OwnerNotEntitled` if the address
    that comes back is a real, decodable owner that simply is not
    ``coldkey``. Returns ``True`` -- never ``False`` -- on a real match, so
    a caller cannot mistake "did not raise" for "was not checked."
    """
    if not isinstance(block_hash, str) or not block_hash.strip():
        raise OwnerReadUnreadable(
            f"confirm_owner_entitled_at_block needs a real block hash to pin its read to, "
            f"got {block_hash!r}. A read with no block named would silently fall back to "
            f"chain head, which is exactly the live-instead-of-historical bug this check "
            f"exists to close."
        )

    try:
        raw_owner = source.owner_at(hotkey=hotkey, block_hash=block_hash)
    except Refusal:
        raise
    except Exception as failure:
        raise OwnerReadUnreadable(
            f"reading Owner[{hotkey}] at block {block_hash} failed: {failure}. Nothing is "
            f"known rather than nothing found."
        ) from failure

    try:
        owner_address = _account_id32_address(raw_owner)
    except ValueError as failure:
        raise OwnerReadUnreadable(
            f"Owner[{hotkey}] at block {block_hash} came back as {raw_owner!r}, which is not "
            f"a real, decodable chain address: {failure}"
        ) from failure

    try:
        owner_key = public_key_of(owner_address)
        coldkey_key = public_key_of(coldkey)
    except NotAnAddress as failure:
        raise OwnerReadUnreadable(
            f"comparing owner {owner_address!r} against depositor {coldkey!r} for hotkey "
            f"{hotkey} at block {block_hash} failed: {failure}"
        ) from failure

    if owner_key != coldkey_key:
        raise OwnerNotEntitled(
            f"{coldkey} is not entitled to assign a deposit's credit to hotkey {hotkey}: "
            f"at block {block_hash}, {hotkey} was owned by {owner_address}, not {coldkey}. "
            f"The depositing wallet was not the one that controlled this hotkey at the time "
            f"of the deposit."
        )
    return True
