"""Historical chain reads for the day scanner.

Queries use explicit block hashes and the current finalized head. Decoders reject
unknown shapes rather than coercing missing or malformed state into zero. Holding
reads distinguish absent storage keys from present zero values. Transport errors
are reported with endpoint credentials redacted; bounded retries handle recognized
rate limits and impossible default replies."""

import logging
import time

from .holdings import from_storage, resolve_denominator, resolve_share
from .sources import Refusal, endpoint_label, hash_canary, scrub_endpoint_text
from .ss58 import NotAnAddress, address_of, public_key_of

_EVENTS_ATTEMPTS = 4
_EVENTS_RETRY_PAUSE_SECONDS = 1.0

# Seconds waited before each re-ask when the endpoint reports its rate limit;
# one more attempt than there are waits (7 attempts, 195 seconds at most).
_RATE_LIMIT_WAITS = (5, 10, 20, 40, 60, 60)
_RATE_LIMIT_MARKER = "rate limit"


def _is_rate_limited(failure):
    """True when the failure text names the endpoint's rate limit (any case).

    Deliberately narrow: only this phrase earns a retry; every other failure
    is refused at once."""
    return _RATE_LIMIT_MARKER in str(failure).lower()


_NULL_REPLY_WAITS = _RATE_LIMIT_WAITS
_guard_logged = set()
_logger = logging.getLogger(__name__)


def _note_guard(kind, block, reply):
    """Log, once per kind per process, that a guard rejected a reply."""
    if kind in _guard_logged:
        return
    _guard_logged.add(kind)
    _logger.warning("read guard: %s at block %s returned %r; re-asking", kind, block, reply)


def _pause(seconds):
    """The pause between re-asks of an empty event list; a module-level name so
    tests patch it instead of really sleeping."""
    time.sleep(seconds)


class ChainUnavailable(Refusal):
    """The chain could not be asked, so nothing is known rather than nothing found."""


class AlphaPriceUnreadable(ChainUnavailable):
    """The alpha spot price at a block could not be read as a positive integer.

    Raised when the node cannot serve that block's state (for example pruned),
    the runtime call fails, or the answer is not a positive plain integer.
    There is no fallback price: a deposit that cannot be priced at its own
    block is refused, never valued at zero or at another block's price.
    """


def storage_item_declared(connection, pallet, name, block_hash):
    """Whether the runtime metadata in force at ``block_hash`` declares
    ``pallet.name`` as a storage item.

    The chain's own answer for the block being read, never a block
    number or a spec version written into this code: runtime upgrade 472 ->
    473 (first applied at block 9,217,264) removed ``SubtensorModule.Alpha``,
    and a hard-coded boundary would be wrong the day the next upgrade moves
    another item. ``get_metadata_storage_function`` loads the runtime for
    that block hash (``init_runtime``) and returns ``None`` for an item the
    pallet (or the whole pallet) does not declare, so ``None`` is the only
    answer that means "not declared".

    Anything else that goes wrong (the endpoint failing, a block it cannot
    serve metadata for, a client error) is NOT an answer and is not caught
    here: it propagates, and the ``_ask`` around every caller refuses it.
    "Could not read the metadata" is never turned into "not declared".

    **Limit of "the metadata at that hash".** A block hash does NOT name
    exactly one runtime as far as the client is concerned: with
    async-substrate-interface 2.2.1, ``init_runtime`` picks the runtime by the
    parent block's spec version and caches it by spec number. Within one spec
    (473 spans the block where ``Alpha`` left the metadata) the metadata
    returned for a hash can therefore depend on which block was loaded first
    on that connection. Practical effect: after a restart or reconnect, two
    sources can disagree about "declared" for the same hash, which surfaces as
    ``SourceDisagreement`` on ``holdingMapsDeclared``. That is a refusal to be
    retried, never a wrong value. The per-chain cache below is keyed by hash
    only to avoid repeated reads; it does not make the answer authoritative.
    """
    entry = connection.get_metadata_storage_function(pallet, name, block_hash=block_hash)
    return entry is not None


def share_map_pair_declared(is_declared, v1_name, v2_name, block_hash):
    """For one old/new pair of share maps at ``block_hash``: ``True`` when the
    old map is declared (so both are read), ``False`` when only the new one is
    (so the old one is genuinely "no such map" and is handed on as ``None``).

    ``is_declared(name)`` answers (or raises) for one item at ``block_hash``.

    Refuses (``ChainUnavailable``) when the new map is not declared, whether
    or not the old one is: ``resolve_share``/``resolve_denominator`` can only
    answer "absent from the old map, look in the new one", and with no new map
    declared there is nowhere honest to look. That covers "neither declared"
    (including a pallet the metadata does not list) and the unexpected
    old-only shape.
    """
    v2_declared = is_declared(v2_name)
    v1_declared = is_declared(v1_name)
    if not v2_declared:
        raise ChainUnavailable(
            f"the runtime metadata at block hash {block_hash} does not declare "
            f"SubtensorModule.{v2_name} (and {v1_name} is "
            f"{'declared' if v1_declared else 'not declared either'}). A wallet's shares can "
            f"only be read from a declared map, so nothing is read and nothing is assumed "
            f"absent."
        )
    return v1_declared


def _plain(value):
    return value.value if hasattr(value, "value") else value


def _mechanism_count_scalar(raw):
    """Decode a plain u8 or a single-element u8 wrapper; reject every other shape."""
    value = _plain(raw)
    if isinstance(value, list):
        if len(value) == 1:
            element = _plain(value[0])
            if _is_plain_mechid_int(element):
                return element
        raise ValueError(
            f"decoded as {value!r}, a list not shaped like the single-element "
            f"MechId wrapper this storage item is confirmed to produce (issue "
            f"#532) -- refusing rather than guessing which element, if any, is "
            f"the real value. Treat this block's mechanism count as unread, "
            f"check whether the pinned runtime's MechId type "
            f"(common/src/lib.rs:216) changed, and report the shape above."
        )
    if _is_plain_mechid_int(value):
        return value
    raise ValueError(
        f"decoded as {value!r}, neither the confirmed single-element list "
        f"shape nor an already-plain int in MechId's own 0-255 range -- "
        f"refusing rather than coercing or accepting an out-of-range value. "
        f"Treat this block's mechanism count as unread, check whether the "
        f"pinned runtime's MechId type (common/src/lib.rs:216) changed, and "
        f"report the value above."
    )


def _is_plain_mechid_int(value):
    """Require the builtin int type and the u8 range; bool and int subclasses are refused."""
    return type(value) is int and 0 <= value <= 255


_ACCOUNT_ID32_BYTES = 32

# AlphaBalance wraps a bare u64 (common/src/currency.rs:31-51). Used by
# hotkey_alpha's own decode guard -- see that method's own docstring.
_U64_BOUND = 2**64


def _first_emission_block_scalar(raw):
    """Decode an absent emission start or a plain u64 block number without coercion."""
    value = _plain(raw)
    if value is None:
        return None
    if type(value) is int and 0 <= value < _U64_BOUND:
        return value
    raise ValueError(
        f"FirstEmissionBlockNumber decoded as {value!r}, neither None (the honest "
        f"OptionQuery absence -- no start call has landed yet) nor an already-plain int "
        f"in u64's own range -- refusing rather than coercing a bool, a numeric string, "
        f"bytes, a float, or an out-of-range int through a bare int() call. Treat this "
        f"block's emission-start read as unread, check whether FirstEmissionBlockNumber's "
        f"declared type (pallets/subtensor/src/lib.rs:2207-2208) changed, and report the "
        f"value above."
    )


IDENTITY_ITEM = "SubnetIdentitiesV3"
ARK_PIN_FIELD = "additional"


def _ark_identity_pin_scalar(raw, *, field):
    """Decode the requested identity field, returning None for absent or empty content."""
    record = _plain(raw)
    if record is None:
        return None
    value = record.get(field) if isinstance(record, dict) else getattr(record, field, None)
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def _account_id32_address(raw):
    """Decode a checksummed SS58 string or a supported 32-byte AccountId wrapper.

    Reject malformed bytes and ambiguous nested shapes rather than stringifying them."""
    value = _plain(raw)

    if isinstance(value, str):
        try:
            public_key = public_key_of(value)
        except NotAnAddress as failure:
            raise ValueError(
                f"decoded as {value!r}, a string but not a genuine, correctly "
                f"checksummed SS58 address ({failure}) -- refusing rather than "
                f"accepting any string. Issue #536's own original bug also produced "
                f"a string (str() of a mis-decoded list); a bare isinstance(x, str) "
                f"check here would accept that exact garbage right back in. Treat "
                f"this block's read as unread and report the value above."
            ) from failure
        return address_of(public_key)

    unwrapped = _account_id32_bytes_from_sequence(value)
    if unwrapped is not None:
        return address_of(unwrapped)

    if isinstance(value, (list, tuple)) and len(value) == 1:
        unwrapped = _account_id32_bytes_from_sequence(_plain(value[0]))
        if unwrapped is not None:
            return address_of(unwrapped)

    if isinstance(value, (list, tuple)):
        raise ValueError(
            f"decoded as {value!r}, a list or tuple not shaped like the "
            f"single-element AccountId32 wrapper this storage item is confirmed to "
            f"produce (issue #536) -- refusing rather than guessing which element, "
            f"if any, is the real 32 bytes. Treat this block's read as unread, "
            f"check whether the "
            f"pinned runtime's AccountId32 type (polkadot-sdk "
            f"substrate/primitives/core/src/crypto.rs:516, pinned at "
            f"Cargo.lock's sp-core rev cacb4310f20c7cac83eb3ccd8ed5a5ad4212608a) "
            f"changed, and report the shape above."
        )
    raise ValueError(
        f"decoded as {value!r}, none of the three confirmed AccountId32 shapes -- "
        f"a single-element list wrapping 32 plain bytes, an already-unwrapped "
        f"32-byte sequence, or a genuine SS58 string (issues #536, #537) -- "
        f"refusing rather than guessing at the 32 raw bytes an SS58 address would "
        f"be built from. Treat this block's read as unread, check whether the "
        f"pinned runtime's AccountId32 type (polkadot-sdk "
        f"substrate/primitives/core/src/crypto.rs:516, pinned at Cargo.lock's "
        f"sp-core rev cacb4310f20c7cac83eb3ccd8ed5a5ad4212608a) changed, and report "
        f"the value above."
    )


def _account_id32_bytes_from_sequence(candidate):
    """32 raw bytes if ``candidate`` is a plain 32-length sequence of plain
    0-255 ints -- OR already a 32-byte ``bytes``/``bytearray``/single-byte-
    itemsize ``memoryview`` -- else ``None``. Never raises, so both
    branches of ``_account_id32_address`` can try their own shape and fall
    through to their own named refusal rather than one generic message
    covering both."""
    if isinstance(candidate, (bytes, bytearray)):
        return bytes(candidate) if len(candidate) == _ACCOUNT_ID32_BYTES else None
    if isinstance(candidate, memoryview):
        if candidate.itemsize == 1 and len(candidate) == _ACCOUNT_ID32_BYTES:
            return bytes(candidate)
        return None
    if not isinstance(candidate, (list, tuple)) or len(candidate) != _ACCOUNT_ID32_BYTES:
        return None
    elements = [_plain(item) for item in candidate]
    if not all(_is_plain_byte(item) for item in elements):
        return None
    return bytes(elements)


def _is_plain_byte(value):
    """Require a builtin int in the byte range; refuse bool and subclasses."""
    return type(value) is int and 0 <= value <= 255


_EVENT_RECORD_KEYS = frozenset(
    {
        "phase",
        "extrinsic_idx",
        "event",
        "event_index",
        "module_id",
        "event_id",
        "attributes",
        "topics",
    }
)


def _event_dict(value):
    """Require a complete decoded EventRecord with nonempty module and event names."""
    plain = _plain(value)
    if type(plain) is dict and _EVENT_RECORD_KEYS <= plain.keys():
        module_id, event_id = plain.get("module_id"), plain.get("event_id")
        if type(module_id) is str and module_id and type(event_id) is str and event_id:
            return plain
        raise ValueError(
            f"one of this block's events decoded with module_id={module_id!r}, "
            f"event_id={event_id!r} -- not the non-empty strings required for "
            f"both identifiers -- refusing rather than letting deposits.py/"
            f"emissions.py's own module_id-routing checks silently treat this as an "
            f"honest unrelated event. Treat this block's events as unread, and "
            f"report the value above."
        )
    if type(plain) is dict:
        raise ValueError(
            f"one of this block's events decoded as a dict missing "
            f"{sorted(_EVENT_RECORD_KEYS - plain.keys())} -- not the complete "
            f"EventRecord shape -- refusing rather than "
            f"merging ordinal_in_block into an incomplete record two modules "
            f"downstream (deposits.py, emissions.py) would otherwise silently skip "
            f"instead of refuse. Treat this block's events as unread, and report "
            f"the value above."
        )
    raise ValueError(
        f"one of this block's events decoded as {plain!r} ({type(plain).__name__}), "
        f"not the genuine dict required for a decoded EventRecord -- "
        f"refusing rather than guessing how to "
        f"merge ordinal_in_block into whatever this is. Treat this block's events "
        f"as unread, check whether the installed async_substrate_interface version "
        f"(>=2.2.1 per pyproject.toml) changed get_events()'s shape, and report the "
        f"shape above."
    )


class LiveRunnerChain:
    """One endpoint, one subnet, answering what ``run_day`` asks of a chain.

    ``connection`` is any client offering ``get_block_number``,
    ``get_block_hash``, ``get_events`` and ``query``/``query_map``, which is
    exactly what ``async_substrate_interface.SubstrateInterface`` (sync facade)
    provides and what ``neurons/miner.py`` already builds one of. ``netuid`` is
    fixed for the life of the adapter: ``run_day``'s per-block protocol asks
    ``seat_hotkeys(block)`` and friends with no netuid argument, because a
    runner reads one subnet at a time.
    """

    def __init__(self, connection, *, netuid, endpoint):
        self._connection = connection
        self.netuid = netuid
        self.endpoint = endpoint
        # (block, raw answer) of this source's own check read, or None. See
        # the rule comment above ``block_hash``.
        self._canary_hash = None
        # (pallet, item, block hash) -> declared?, successful reads only. Keyed by
        # hash because this class is never told a spec number; see the limit
        # in ``storage_item_declared``'s docstring. An error is never stored.
        self._declared = {}

    def close(self):
        """Close the underlying connection or connections. Cleanup failures remain visible."""
        self._connection.close()

    # -- the four questions asked of every block -----------------------------

    def head_block(self):
        """Return the current finalized head block number."""

        def _current_head():
            head_hash = self._connection.get_chain_finalised_head()
            return self._connection.get_block_number(head_hash)

        return self._ask("the chain head", _current_head)

    # Rule: a hash read directly after this source's own check read of the
    # same block is the same read; every other hash read is fresh.
    #
    # A dual-source question first reads each source's block hash as its check
    # (``canary``) and then asks the question, whose method starts by reading
    # the hash of that same block from that same source again. Only that one
    # adjacent second read is served from the check read. The memo is
    # instance-local, holds one (block, answer) pair, is consumed by the very
    # next ``block_hash`` call whether or not it matches, and is cleared on
    # entry to ``_ask`` (so any other question in between invalidates it).
    # Nothing is kept by height and nothing is shared between sources. The
    # check read itself always goes to the connection, is still compared
    # across sources by ``AgreeingChain``, and is the very read the question's
    # answer is built on. Later deliberate re-reads arrive as new questions,
    # each starting with a fresh check read.

    def _read_block_hash(self, block):
        return self._ask(
            f"the hash of block {block}", lambda: self._connection.get_block_hash(block)
        )

    def block_hash(self, block):
        memo, self._canary_hash = self._canary_hash, None
        if memo is not None and memo[0] == block and type(memo[0]) is type(block):
            return memo[1]
        return self._read_block_hash(block)

    def block_timestamp(self, block):
        block_hash = self.block_hash(block)
        return self._ask(
            f"the time of block {block}",
            lambda: (
                self._ask_unless_default(
                    "timestamp",
                    block,
                    lambda: _plain(
                        self._connection.query("Timestamp", "Now", [], block_hash=block_hash)
                    ),
                    lambda raw: block >= 1 and (raw is None or raw == 0),
                )
                // 1000
            ),
        )

    def _ask_unless_default(self, kind, block, read, impossible):
        """Ask ``read()``; a reply for which ``impossible(reply)`` holds is no answer.

        Such a reply is the storage default the client library substitutes for
        a null reply, which cannot be the truth for this block. The question is
        re-asked after each wait in ``_NULL_REPLY_WAITS``; if every attempt is
        impossible, ``ChainUnavailable`` names the block, the kind and the reply.
        """
        reply = read()
        for wait in _NULL_REPLY_WAITS:
            if not impossible(reply):
                return reply
            _note_guard(kind, block, reply)
            _pause(wait)
            reply = read()
        if impossible(reply):
            raise ChainUnavailable(
                f"the {kind} of block {block} came back as {reply!r} on all "
                f"{len(_NULL_REPLY_WAITS) + 1} attempts. That value cannot be the answer "
                f"for this block, so it is no answer: nothing is KNOWN rather than "
                f"nothing FOUND."
            )
        return reply

    def events(self, block):
        """Return the complete event list at the requested block, with stable ordinals.

        Retry impossible empty replies within a bound, then refuse instead of recording
        a falsely empty block."""
        block_hash = self.block_hash(block)

        def question():
            for attempt in range(1, _EVENTS_ATTEMPTS + 1):
                raw = _plain(self._connection.get_events(block_hash=block_hash))
                if not isinstance(raw, (list, tuple)):
                    raise ChainUnavailable(
                        f"the events of block {block} came back as {raw!r}, not a list of events."
                    )
                if raw:
                    break
                if attempt == 1:
                    _note_guard("events", block, raw)
                if attempt < _EVENTS_ATTEMPTS:
                    _pause(_EVENTS_RETRY_PAUSE_SECONDS)
            else:
                raise ChainUnavailable(
                    f"the events of block {block} came back empty on all "
                    f"{_EVENTS_ATTEMPTS} attempts (raw reply length 0). A real block always "
                    f"has events, so an empty list is no answer: nothing is KNOWN rather "
                    f"than nothing FOUND."
                )
            return [
                dict(_event_dict(event), ordinal_in_block=position)
                for position, event in enumerate(raw)
            ]

        return self._ask(f"the events of block {block}", question)

    # -- the canary every source proves itself with, every block -------------

    def canary(self, block):
        """Read the block hash used to verify a source before trusting its other answers."""
        value = self._read_block_hash(block)  # always a fresh connection read
        self._canary_hash = (block, value)
        return hash_canary(value)


    def seat_hotkeys(self, block):
        return self._ask(
            f"Keys[{self.netuid}] map at block {block}",
            lambda: {int(_plain(k)): _account_id32_address(v) for k, v in self._map("Keys", block)},
        )

    def registration_blocks(self, block):
        return self._ask(
            f"BlockAtRegistration[{self.netuid}] map at block {block}",
            lambda: {
                int(_plain(k)): int(_plain(v)) for k, v in self._map("BlockAtRegistration", block)
            },
        )

    def subnetwork_n(self, block):
        return self._ask(
            f"SubnetworkN[{self.netuid}] at block {block}",
            lambda: int(_plain(self._storage("SubnetworkN", block))),
        )

    def mechanism_count(self, block):
        return self._ask(
            f"MechanismCountCurrent[{self.netuid}] at block {block}",
            lambda: _mechanism_count_scalar(self._storage("MechanismCountCurrent", block)),
        )

    def incentive(self, block):
        """Read Incentive at this subnet's storage index.

        This reader addresses mechanism zero. It does not yet accept a separate mechanism
        index for subnets that run several mechanisms."""
        return self._ask(
            f"Incentive[{self.netuid}] at block {block}",
            lambda: [int(v) for v in _plain(self._storage("Incentive", block))],
        )

    def hotkey_owners(self, hotkeys, block):
        block_hash = self.block_hash(block)
        out = {}
        for hotkey in hotkeys:
            out[hotkey] = self._ask(
                f"the owner of {hotkey} at block {block}",
                lambda hotkey=hotkey: _account_id32_address(
                    self._connection.query(
                        "SubtensorModule", "Owner", [hotkey], block_hash=block_hash
                    )
                ),
            )
        return out

    def subnet_owner(self, block):
        return self._ask(
            f"SubnetOwner/SubnetOwnerHotkey[{self.netuid}] at block {block}",
            lambda: (
                _account_id32_address(self._storage("SubnetOwner", block)),
                _account_id32_address(self._storage("SubnetOwnerHotkey", block)),
            ),
        )


    def extrinsics(self, block):
        """Read this block's decoded extrinsics, preserving the indexes used by events."""
        block_hash = self.block_hash(block)

        def read():
            block_data = self._connection.get_block(block_hash=block_hash)
            if not isinstance(block_data, dict) or not isinstance(
                block_data.get("extrinsics"), (list, tuple)
            ):
                raise ChainUnavailable(
                    f"the block at {block_hash} (block {block}) came back as {block_data!r}, "
                    f"not a decoded record carrying an 'extrinsics' list."
                )
            return [_plain(entry) for entry in block_data["extrinsics"]]

        def question():
            # Every block after genesis carries the timestamp inherent.
            return self._ask_unless_default(
                "extrinsics", block, read, lambda reply: block >= 1 and not reply
            )

        return self._ask(f"the extrinsics of block {block}", question)

    def owner_at(self, *, hotkey, block_hash, block=None):
        """Read ownership at the caller-specified historical block hash, never at head.

        An unavailable ownership query is a read refusal, not proof of absent ownership."""

        try:
            raw_owner = self._connection.query(
                "SubtensorModule", "Owner", [hotkey], block_hash=block_hash
            )
        except Exception as failure:
            reason = scrub_endpoint_text(failure, self.endpoint)
            raise ChainUnavailable(
                f"asking {endpoint_label(self.endpoint)} for the owner of {hotkey} at block hash "
                f"{block_hash} failed: {reason}. Nothing is KNOWN rather than nothing "
                f"FOUND, and treating this as an ordinary decode failure would record a deposit "
                f"as unattributed "
                f"when the real problem is that the chain could not be asked at all."
            ) from None
        return _account_id32_address(raw_owner)


    def staking_hotkeys(self, coldkey, block):
        """Every hotkey this coldkey stakes to, on this subnet or any other.

        ``StakingHotkeys<T> = StorageMap<_, Blake2_128Concat, T::AccountId,
        Vec<T::AccountId>, ValueQuery>`` (pallets/subtensor/src/lib.rs:1709-1711,
        spec 450) -- a plain ``Vec``, not a struct wrapping one, so none of the
        composite-decode guards elsewhere in this file apply: a ``Vec<T>`` is
        this project's read path's most ordinary shape, a genuine Python list,
        unlike the single-field tuple structs ``_mechanism_count_scalar`` and
        ``_account_id32_address`` exist to unwrap. ``holding_for_coldkey``'s own
        docstring (``noah/chain/holdings.py``) names this as the exact set the
        runtime itself sums in ``total_coldkey_alpha_on_subnet``
        (staking/lock.rs:702-710) -- whoever calls this owes the caller that
        list, unfiltered by netuid, because the runtime doesn't filter it
        either; ``alpha_share``/``hotkey_shares`` below are what narrow a
        hotkey's contribution to THIS subnet.

        No presence/absence question here, unlike the three methods below:
        ``ValueQuery`` with no custom default means an absent key and a key
        holding an explicit empty list decode identically (``[]``), and both
        mean the same honest thing -- this coldkey stakes to nothing -- so
        there is nothing a raw-key check could tell apart.
        """
        block_hash = self.block_hash(block)

        def question():
            raw = _plain(
                self._connection.query(
                    "SubtensorModule", "StakingHotkeys", [coldkey], block_hash=block_hash
                )
            )
            if not isinstance(raw, (list, tuple)):
                raise ChainUnavailable(
                    f"StakingHotkeys[{coldkey}] decoded as {raw!r}, not a list of hotkeys. "
                    f"That map's value is a Vec<AccountId>, which this project's read path "
                    f"renders as a list everywhere else it appears -- refusing rather than "
                    f"guessing how to read a wallet's hotkeys out of anything else."
                )
            return [_account_id32_address(item) for item in raw]

        return self._ask(f"StakingHotkeys[{coldkey}] at block {block}", question)

    def alpha_share(self, hotkey, coldkey, block):
        """A wallet's own share of one hotkey's pool, in whichever number
        system it is actually stored in.

        Reads both ``Alpha`` (old, ``U64F64``) and ``AlphaV2`` (new,
        ``SafeFloat``) -- both NMaps over ``(hot, cold, netuid)``, in that
        order (pallets/subtensor/src/lib.rs:1809-1819 and 1834-1844, spec
        450) -- and hands both straight to ``holdings.py``'s ``resolve_share``,
        which is the only place either is interpreted. See ``_share_component``
        below for how presence (not just the decoded value) is read for each,
        and ``resolve_share``'s own docstring for why an absent old key must
        never be treated the same as an old key holding zero.
        """
        v1_bits, v2_parts = self._share_pair(
            "Alpha", "AlphaV2", [hotkey, coldkey, self.netuid], block
        )
        return resolve_share(v1_bits=v1_bits, v2_parts=v2_parts)

    def hotkey_shares(self, hotkey, block):
        """Read a hotkey pool's total shares using the declared storage encoding.

        The denominator must be consistent with the wallet share encoding and presence."""
        v1_bits, v2_parts = self._share_pair(
            "TotalHotkeyShares", "TotalHotkeySharesV2", [hotkey, self.netuid], block
        )
        return resolve_denominator(v1_bits=v1_bits, v2_parts=v2_parts, share_present=False)

    def hotkey_alpha(self, hotkey, block):
        """Read the hotkey pool's alpha amount as a plain u64; reject malformed replies."""
        block_hash = self.block_hash(block)

        def question():
            value = _plain(
                self._connection.query(
                    "SubtensorModule",
                    "TotalHotkeyAlpha",
                    [hotkey, self.netuid],
                    block_hash=block_hash,
                )
            )
            if type(value) is int and 0 <= value < _U64_BOUND:
                return value
            raise ValueError(
                f"TotalHotkeyAlpha[{hotkey},{self.netuid}] decoded as {value!r}, not the "
                f"already-plain int in AlphaBalance's u64 range -- refusing rather than "
                f"coercing a bool, a numeric string, bytes, a float, an out-of-range int, "
                f"or a list wrapper (the shape a structurally similar type, MechId, is "
                f"confirmed to need unwrapping for -- see this method's own docstring) "
                f"through a bare int() call. Treat this block's pool alpha as unread, "
                f"check whether AlphaBalance's decode shape changed, and report the "
                f"value above."
            )

        return self._ask(f"TotalHotkeyAlpha[{hotkey},{self.netuid}] at block {block}", question)

    def holding_maps_declared(self, block):
        """Which old share maps the runtime at ``block`` still declares.

        ``{"Alpha": bool, "TotalHotkeyShares": bool}``, read from the chain's
        metadata at that block's hash (see ``storage_item_declared``), refused
        unless the new map of each pair is declared. Exists so that
        ``AgreeingChain`` can make the two sources AGREE on which maps exist:
        two sources that disagree about the metadata must not have their
        (possibly coinciding) share answers accepted.
        """
        block_hash = self.block_hash(block)
        return self._ask(
            f"which holding maps the runtime declares at block {block}",
            lambda: {
                "Alpha": self._pair_declared("Alpha", "AlphaV2", block_hash),
                "TotalHotkeyShares": self._pair_declared(
                    "TotalHotkeyShares", "TotalHotkeySharesV2", block_hash
                ),
            },
        )

    def _item_declared(self, name, block_hash):
        key = ("SubtensorModule", name, block_hash)
        if key not in self._declared:
            if len(self._declared) >= 4096:
                self._declared.clear()
            self._declared[key] = storage_item_declared(
                self._connection, "SubtensorModule", name, block_hash
            )
        return self._declared[key]

    def _pair_declared(self, v1_name, v2_name, block_hash):
        return share_map_pair_declared(
            lambda name: self._item_declared(name, block_hash), v1_name, v2_name, block_hash
        )

    def _share_pair(self, v1_name, v2_name, params, block):
        """One old/new pair of share components at ONE block hash.

        The hash is read once, and the metadata question and both storage
        reads all use it. The old map is read only when the metadata at that
        hash declares it; otherwise it is ``None``, the resolvers' "no such
        key" (an absent key in a DECLARED map keeps its existing meaning, see
        ``_share_component``). The new map is always read.
        """
        block_hash = self.block_hash(block)
        v1_declared = self._ask(
            f"whether {v1_name}/{v2_name} are declared at block {block}",
            lambda: self._pair_declared(v1_name, v2_name, block_hash),
        )
        v1 = (
            self._share_component(v1_name, params, block, block_hash=block_hash)
            if v1_declared
            else None
        )
        v2 = self._share_component(v2_name, params, block, block_hash=block_hash)
        return v1, v2

    def _share_component(self, name, params, block, block_hash=None):
        """Read a historical share component and independently check raw key presence.

        A default decoded zero does not establish absence; the raw storage key determines
        whether this is a present value or no entry."""
        if block_hash is None:
            block_hash = self.block_hash(block)

        def question():
            decoded = _plain(
                self._connection.query("SubtensorModule", name, list(params), block_hash=block_hash)
            )
            storage_key = self._connection.create_storage_key(
                "SubtensorModule", name, list(params), block_hash=block_hash
            )
            present = (
                self._connection.get_storage_by_key(block_hash, storage_key.to_hex()) is not None
            )
            return from_storage(decoded, present)

        return self._ask(f"{name}{list(params)} at block {block}", question)


    def first_emission_block(self, block):
        """``SubtensorModule.FirstEmissionBlockNumber[netuid]`` at ``block``:
        the real block the subnet owner's start call recorded, or ``None``
        if no start call has landed yet, as of this exact block.

        See ``_first_emission_block_scalar``'s own docstring for the full
        decode reasoning and the pinned runtime citation. This method
        follows the identical "wrap the ENTIRE body in one ``_ask`` call"
        shape ``subnetwork_n``/``mechanism_count`` above already use, so a
        decode failure is reported by this exact read's own name rather
        than surfacing as an unprotected ``ValueError`` two layers up.
        """
        return self._ask(
            f"FirstEmissionBlockNumber[{self.netuid}] at block {block}",
            lambda: _first_emission_block_scalar(self._storage("FirstEmissionBlockNumber", block)),
        )


    def published_ark_pin(self, block, *, field=ARK_PIN_FIELD):
        """The Ark address this subnet's own identity record publishes in
        ``field``, at ``block`` -- or ``None`` if no identity record exists
        yet at this block. ``None`` is the honest ``OptionQuery`` absence,
        never a mismatch: a subnet that has not published a record has not
        disagreed with anything, and the caller that compares this against
        an expected address is responsible for telling the two apart (see
        ``noah.core.chain_adapter``'s own ``ArkIdentityNotPublished`` vs
        ``ArkIdentityMismatch``).

        See ``_ark_identity_pin_scalar``'s own docstring for the full decode
        reasoning and the pinned runtime citation, and
        ``noah.chain.agreeing.AgreeingChain.published_ark_pin`` for the
        dual-sourced sibling ``ChainAdapter.read()`` actually calls -- this
        single-source method is what that one wraps, the identical relation
        ``first_emission_block`` above already has to its own agreeing
        counterpart.
        """
        return self._ask(
            f"SubnetIdentitiesV3[{self.netuid}].{field} at block {block}",
            lambda: _ark_identity_pin_scalar(self._storage(IDENTITY_ITEM, block), field=field),
        )


    def collateral_arity(self, block):
        """Read collateral map key arity from runtime metadata, including for empty maps."""
        block_hash = self.block_hash(block)

        def one(name):
            entry = self._connection.get_metadata_storage_function(
                "SubtensorModule", name, block_hash=block_hash
            )
            value = entry.value if hasattr(entry, "value") else entry
            hashers = None
            if isinstance(value, dict):
                hashers = (value.get("type") or {}).get("Map", {}).get("hashers")
            if hashers is None:
                raise ChainUnavailable(
                    f"the runtime's description of {name} at block {block} carries no key "
                    f"definition, so the shape of every question built for it is unknown. "
                    f"Nothing is read."
                )
            return len(hashers)

        return self._ask(
            f"collateral map arity at block {block}",
            lambda: {
                "ColdkeyMinerCollateral": one("ColdkeyMinerCollateral"),
                "MinerCollateral": one("MinerCollateral"),
            },
        )

    def coldkey_collateral(self, coldkey, block):
        """Read the coldkey's aggregate collateral lock at the specified block.

        The storage default is treated as zero; non-default values must be plain u64."""
        block_hash = self.block_hash(block)

        def question():
            value = _plain(
                self._connection.query(
                    "SubtensorModule",
                    "ColdkeyMinerCollateral",
                    [self.netuid, coldkey],
                    block_hash=block_hash,
                )
            )
            if value is None:
                return 0
            if type(value) is int and 0 <= value < _U64_BOUND:
                return value
            raise ValueError(
                f"ColdkeyMinerCollateral[{self.netuid},{coldkey}] decoded as {value!r}, not "
                f"None (ValueQuery's own absent-row default) or an already-plain int in "
                f"AlphaBalance's own u64 range -- refusing rather than coercing a bool, a "
                f"numeric string, bytes, a float, or an out-of-range int through a bare "
                f"int() call. Treat this wallet's locked collateral as unread, check "
                f"whether AlphaBalance's decode shape changed, and report the value above."
            )

        return self._ask(
            f"ColdkeyMinerCollateral[{self.netuid},{coldkey}] at block {block}", question
        )

    def collateral_hotkeys(self, coldkey, block):
        """Read and validate all hotkeys with collateral positions for this coldkey."""
        block_hash = self.block_hash(block)

        def question():
            raw = _plain(
                self._connection.query(
                    "SubtensorModule",
                    "ColdkeyCollateralHotkeys",
                    [self.netuid, coldkey],
                    block_hash=block_hash,
                )
            )
            if raw is None:
                return []
            if not isinstance(raw, (list, tuple)):
                raise ChainUnavailable(
                    f"ColdkeyCollateralHotkeys[{self.netuid},{coldkey}] decoded as {raw!r}, "
                    f"not a list of hotkeys. Reading this as an empty hotkey set would "
                    f"understate pledged collateral -- refusing rather than guessing."
                )
            try:
                return [_account_id32_address(item) for item in raw]
            except ValueError as failure:
                raise ChainUnavailable(
                    f"ColdkeyCollateralHotkeys[{self.netuid},{coldkey}] decoded as {raw!r}, "
                    f"containing an element that is not a genuine hotkey ({failure}). "
                    f"Silently dropping it would shrink this coldkey's hotkey set and "
                    f"understate pledged collateral."
                ) from failure

        return self._ask(
            f"ColdkeyCollateralHotkeys[{self.netuid},{coldkey}] at block {block}", question
        )

    def miner_collateral(self, hotkey, coldkey, block):
        """Read the collateral row for (netuid, hotkey, coldkey) at the pinned block."""
        block_hash = self.block_hash(block)
        return self._ask(
            f"MinerCollateral[{self.netuid},{hotkey},{coldkey}] at block {block}",
            lambda: _plain(
                self._connection.query(
                    "SubtensorModule",
                    "MinerCollateral",
                    [self.netuid, hotkey, coldkey],
                    block_hash=block_hash,
                )
            ),
        )

    # -- how anything is asked ------------------------------------------------

    def alpha_price_at(self, block, netuid=None, *, expected_block_hash=None):
        """Read a subnet's positive integer spot alpha price at exactly the requested block.

        ``netuid`` defaults to this chain's own subnet; a foreign-token deposit is
        valued at its own subnet's price (issue #16).

        Never substitute a current-head price for an unavailable historical price."""
        netuid = self.netuid if netuid is None else netuid
        if isinstance(netuid, bool) or not isinstance(netuid, int) or netuid < 0:
            raise AlphaPriceUnreadable(f"subnet {netuid!r} is not a subnet number.")
        block_hash = self.block_hash(block)
        if expected_block_hash is not None and block_hash != expected_block_hash:
            raise AlphaPriceUnreadable(
                f"block {block} resolves to {block_hash!r}, but the receipt records "
                f"{expected_block_hash!r}. No price is read from a different block."
            )
        if not isinstance(block_hash, str) or not block_hash:
            # chain_getBlockHash answers null for a block that does not exist
            # yet; runtime_call(block_hash=None) would then read at the head.
            raise AlphaPriceUnreadable(
                f"the hash of block {block} is unknown ({block_hash!r}): the block does "
                f"not exist yet or {endpoint_label(self.endpoint)} does not serve it. "
                f"No price is read and the head price is never substituted."
            )
        try:
            raw = _plain(
                self._connection.runtime_call(
                    "SwapRuntimeApi",
                    "current_alpha_price",
                    [(netuid,)],
                    block_hash=block_hash,
                )
            )
        except Refusal:
            raise
        except Exception as failure:
            reason = scrub_endpoint_text(failure, self.endpoint)
            raise AlphaPriceUnreadable(
                f"asking {endpoint_label(self.endpoint)} for the alpha price of netuid "
                f"{netuid} at block {block} ({block_hash}) failed: "
                f"{reason}. No other price is used."
            ) from None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise AlphaPriceUnreadable(
                f"the alpha price of netuid {netuid} at block {block} came back as "
                f"{raw!r}, not a positive integer. No other price is used."
            )
        return raw

    def _storage(self, name, block):
        block_hash = self.block_hash(block)
        return self._ask(
            f"{name}[{self.netuid}] at block {block}",
            lambda: self._connection.query(
                "SubtensorModule", name, [self.netuid], block_hash=block_hash
            ),
        )

    def _map(self, name, block):
        block_hash = self.block_hash(block)
        result = self._ask(
            f"{name}[{self.netuid}] map at block {block}",
            lambda: self._connection.query_map(
                "SubtensorModule", name, [self.netuid], block_hash=block_hash, page_size=1000
            ),
        )
        return list(result)

    def _ask(self, what, question):
        """Every read goes through here, so the refusal exists in ONE place.

        Matches ``noah/deposits/live.py``'s ``LiveChain._ask``: a failed read
        must never be reported as an empty one, because on this path an empty
        answer -- no events, no pay, no deposit -- is a real and different
        thing from a read that never happened.

        A public archive endpoint limits historical reads and answers a read
        over its limit with a "rate limit" error. That is a wait-and-ask-again
        condition, not an answer: the same question is re-asked after the
        waits in ``_RATE_LIMIT_WAITS``, and only if every attempt is limited
        is ``ChainUnavailable`` raised (saying how often it was retried). Any
        other failure is refused at once, with no retry.
        """
        self._canary_hash = None  # any other question invalidates the check-read memo
        retries = 0
        while True:
            try:
                return question()
            except Refusal:
                raise
            except Exception as failure:
                if _is_rate_limited(failure) and retries < len(_RATE_LIMIT_WAITS):
                    _pause(_RATE_LIMIT_WAITS[retries])
                    retries += 1
                    continue
                limited = ""
                if retries and _is_rate_limited(failure):
                    limited = (
                        f" (the endpoint is rate limiting: asked {retries + 1} times, "
                        f"retried {retries} times over {sum(_RATE_LIMIT_WAITS[:retries])} "
                        f"seconds of waiting)"
                    )
                reason = scrub_endpoint_text(failure, self.endpoint)
                raise ChainUnavailable(
                    f"asking {endpoint_label(self.endpoint)} for {what} failed: "
                    f"{reason}{limited}. Nothing is KNOWN rather than nothing "
                    f"FOUND, and a scan that read this as an empty answer would record a day as "
                    f"scanned when part of it was never "
                    f"actually read."
                ) from None
