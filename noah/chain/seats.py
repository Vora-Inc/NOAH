"""Read the seat registry as {coldkey: {hotkey: uid}} at a pinned block.

Keep multiple miner hotkeys under one coldkey distinct. Validate the seat count,
UID mapping, registration and ownership inputs before exposing a scoring roster."""

from .sources import Refusal
from .ss58 import address_of

#: The SS58 text a failed/absent ``Owner`` read decodes to: 32 zero bytes,
#: encoded through this project's own ``address_of`` -- the identical
#: function ``noah.chain.live._account_id32_address`` uses for every real
#: decode, so this is guaranteed to be the exact string that function would
#: ever produce for an all-zero-byte read, not a second, independently
#: computed guess at it. extract.py's own ``_NULL_ACCOUNT``
#: (``ss58_encode(bytes(32), ss58_format=SS58_FORMAT)``) is the identical
#: value under a different encoder; not imported from there because this
#: module routes through this project's own ``ss58.py`` rather than
#: extract.py's standalone one (see this module's own docstring).
_NULL_ACCOUNT = address_of(bytes(32))


class SeatOwnerUnreadable(Refusal):
    """A registered hotkey's ``Owner`` read came back null -- refused rather
    than filed under the fake null account."""


class DuplicateSeatHotkey(Refusal):
    """Two uids report the same hotkey at one block -- ``Owner`` is a 1:1
    map, so this is a genuine chain inconsistency, refused rather than
    picked."""


class SeatSnapshotUnstable(Refusal):
    """The chain's own hash for ``block`` changed between the Keys read and
    the Owner read -- a reorg landed mid-read, and the two maps may span two
    different chain states. Refused rather than trusted; see this module's
    own docstring, "A third guard"."""


def current_seats(runner_chain, block):
    """Every current seat per coldkey at ``block``: ``{coldkey: {hotkey: uid}}``.

    ``runner_chain`` is asked exactly two questions: ``seat_hotkeys(block)``
    (``{uid: hotkey}``, the ``Keys`` map) and ``hotkey_owners(hotkeys,
    block)`` (``{hotkey: coldkey}``, the ``Owner`` map for exactly the
    hotkeys ``Keys`` named -- never the whole chain, matching
    ``noah/chain/runner.py``'s own identical narrowing for the paid-hotkey
    case). Both are already dual-sourced and canary-checked by
    ``AgreeingChain`` itself (or by whatever fake exercises the identical
    protocol in a test); nothing here reads a chain client a second, less
    careful way.

    ``block_hash(block)`` is read once immediately before the Keys read and
    once immediately after the Owner read, and the two are compared: an
    identical hash proves the chain's own state at ``block`` did not move
    during this whole read, so Keys and Owner are provably the same
    snapshot extract.py's own single-hash pin already guaranteed (see this
    module's own docstring, "A third guard").
    """
    before_hash = runner_chain.block_hash(block)
    seat_hotkeys = runner_chain.seat_hotkeys(block)
    hotkeys = sorted(set(seat_hotkeys.values()))
    owners = runner_chain.hotkey_owners(hotkeys, block)
    after_hash = runner_chain.block_hash(block)
    if before_hash != after_hash:
        raise SeatSnapshotUnstable(
            f"block {block}: its own hash read as {before_hash!r} before the seat "
            f"registry read and {after_hash!r} immediately after -- the chain moved "
            f"during this read. Keys and Owner may now describe two different chain "
            f"states, and extract.py's own equivalent read pins both to one resolved "
            f"hash for exactly this reason. Nothing was attributed; re-run once the "
            f"chain has settled."
        )

    seats = {}
    seen_hotkeys = {}
    for uid in sorted(seat_hotkeys):
        hotkey = seat_hotkeys[uid]
        if hotkey in seen_hotkeys:
            raise DuplicateSeatHotkey(
                f"block {block}: hotkey {hotkey!r} is registered to both uid "
                f"{seen_hotkeys[hotkey]} and uid {uid} (the Keys map). Owner is a 1:1 "
                f"map at any given block, so the same hotkey cannot really belong to "
                f"two uids at once; scoring both as separate current seats would "
                f"misattribute one of them. This may indicate a genuine chain "
                f"inconsistency at this block; investigate before retrying."
            )
        seen_hotkeys[hotkey] = uid
        coldkey = owners.get(hotkey)
        if coldkey is None or coldkey == _NULL_ACCOUNT:
            raise SeatOwnerUnreadable(
                f"block {block}: hotkey {hotkey!r} (uid {uid}) has a registered key "
                f"(Keys) but Owner({hotkey!r}) returned nothing (or the all-zero "
                f"default a ValueQuery answers with when the read did not happen) at "
                f"this block. This cannot attribute this current seat to a coldkey and "
                f"refuses to guess -- treating it as unowned would silently misfile a "
                f"real, currently-held seat. This may indicate a genuine chain "
                f"inconsistency at this block; investigate before retrying."
            )
        seats.setdefault(coldkey, {})[hotkey] = uid
    return seats
