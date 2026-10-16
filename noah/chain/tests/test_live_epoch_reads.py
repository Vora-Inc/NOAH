"""``LiveRunnerChain``'s five epoch-block reads, decode-shape focused
(issue #532).

``test_live.py`` covers ``canary`` in depth already; this file is the
sibling audit issue #532 itself asked for: every ``int(_plain(...))``-shaped
read in ``live.py``, checked against fake connections shaped like what the
real chain actually returns, not just fakes that were always going to
return plain ints (the exact gap that let this bug sit byte-identical and
untested for hours -- see ``live.py``'s own module comment above
``seat_hotkeys``).

**The crash, reproduced exactly, before the fix is trusted.** issue #532's
own cited crash text is ``int() argument must be a string, a bytes-like
object or a number, not 'list'`` -- ``test_the_old_naive_int_plain_shape_is_
what_actually_crashed`` reproduces that verbatim from the pre-fix
expression (``int(_plain(raw))``) itself, not from a description of it, so
a reader can see the defect and the fix's guard side by side.
"""

import hashlib

import pytest

from noah.chain.live import (
    ChainUnavailable,
    LiveRunnerChain,
    _account_id32_address,
    _first_emission_block_scalar,
    _mechanism_count_scalar,
    _plain,
)
from noah.chain.ss58 import NotAnAddress, _base58_text, public_key_of

_HASH = "0x" + "ab" * 32


class _ScaleValue:
    """Stands in for a ``scalecodec``/``bt_decode`` decoded object: the
    real client hands back an object with a ``.value`` attribute, not a
    bare Python value -- ``_plain`` is what unwraps it. Used here so the
    fakes below match the real shape ``_plain`` is written to handle,
    including the double-wrapped case (a list whose own element is itself
    one of these) issue #532's real read produced."""

    def __init__(self, value):
        self.value = value


class _Connection:
    """Enough of a raw substrate connection for the epoch-block reads:
    ``get_block_hash`` and ``query``/``query_map``. ``storage`` maps
    ``(module, name)`` to whatever ``query`` should hand back for it --
    shaped however a test needs, including the exact ``[1]`` list issue
    #532 found live."""

    def __init__(self, *, block_hash=_HASH, storage=None, maps=None):
        self._block_hash = block_hash
        self._storage = dict(storage or {})
        self._maps = dict(maps or {})

    def get_block_hash(self, block):
        return self._block_hash

    def query(self, module, name, params, block_hash=None):
        return self._storage[(module, name)]

    def query_map(self, module, name, params, block_hash=None, page_size=None):
        return self._maps[(module, name)]


def _chain(**kwargs):
    return LiveRunnerChain(_Connection(**kwargs), netuid=2, endpoint="wss://node.example")


# -- the crash, reproduced from the pre-fix expression itself ---------------


def test_the_old_naive_int_plain_shape_is_what_actually_crashed():
    """``live.py`` before this fix asked ``mechanism_count`` for
    ``int(_plain(self._storage(...)))`` -- the exact same two calls
    ``_mechanism_count_scalar`` now wraps. Proven against the literal wire
    shape issue #532 reported live (a bare Python list, ``[1]``, not a
    scalecodec object -- ``_plain`` passes a plain list through unchanged,
    which is what made the bare ``int()`` call fail)."""
    with pytest.raises(TypeError, match=r"not 'list'"):
        int(_plain([1]))


# -- mechanism_count: the confirmed real shape is handled --------------------


def test_mechanism_count_unwraps_the_confirmed_single_element_list():
    """The exact shape issue #532's live read returned six times running
    (blocks 1, 100, 137, 138, 140, head): a bare one-element list."""
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): [1]})
    assert chain.mechanism_count(137) == 1


def test_mechanism_count_unwraps_through_the_real_scalecodec_wrapper_shape():
    """The realistic full shape: ``query()`` hands back a scale-object
    whose ``.value`` is the list, and the list's own element is ITSELF a
    scale-object with a ``.value`` -- both layers of ``_plain`` exercised,
    not just the outer one."""
    chain = _chain(
        storage={
            ("SubtensorModule", "MechanismCountCurrent"): _ScaleValue([_ScaleValue(1)]),
        }
    )
    assert chain.mechanism_count(137) == 1


def test_mechanism_count_accepts_a_larger_honest_mechanism_count():
    """Not hard-coded to 1 -- MechId is a u8, so any small int in the
    confirmed single-element-list shape is the honest answer."""
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): [5]})
    assert chain.mechanism_count(137) == 5


def test_mechanism_count_accepts_a_bare_scalar_too():
    """If a future client version stops wrapping single-field composites,
    the already-decoded honest int must still work -- nothing left to
    unwrap, so it is returned unchanged rather than treated as a shape
    the guard doesn't recognize."""
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): 1})
    assert chain.mechanism_count(137) == 1


@pytest.mark.parametrize("boundary", [0, 255])
def test_mechanism_count_accepts_both_ends_of_mechid_s_real_u8_range(boundary):
    """[codex adversarial challenge round 2, 2026-08-29] MechId wraps a u8
    (common/src/lib.rs:216), so 0 and 255 are the honest extremes a real
    decode can produce -- neither end should be mistaken for an
    out-of-range value."""
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): [boundary]})
    assert chain.mechanism_count(137) == boundary


# -- mechanism_count: everything that is NOT the confirmed shape refuses ----


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="empty_list"),
        pytest.param([1, 2], id="two_element_list"),
        pytest.param([[1]], id="nested_list"),
        pytest.param(["1"], id="single_element_but_a_string_not_an_int"),
        pytest.param([True], id="single_element_but_a_bool_not_a_plain_int"),
        pytest.param([256], id="single_element_but_one_past_mechid_s_u8_range"),
        pytest.param([-1], id="single_element_but_negative"),
    ],
)
def test_mechanism_count_refuses_every_shape_that_is_not_the_confirmed_one(raw):
    """The issue's own warning: unwrapping length-1-list-to-its-element is
    safe ONLY because that exact shape is confirmed real for THIS storage
    item. Any other list-like shape -- a 2-element list, empty, nested, a
    single element that isn't itself a plain int, or one outside MechId's
    own u8 range -- must refuse loudly, not be guessed at, or a genuinely
    wrong future decode would be silently accepted the same way [1] now
    correctly is."""
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): raw})
    with pytest.raises(ChainUnavailable, match="not shaped like the single-element"):
        chain.mechanism_count(137)


def test_mechanism_count_scalar_directly_names_the_bad_shape_in_its_refusal():
    """The refusal names WHAT it saw, not just that it refused -- so a
    live failure is diagnosable from the message alone."""
    with pytest.raises(ValueError, match=r"decoded as \[1, 2\]"):
        _mechanism_count_scalar([1, 2])


def test_mechanism_count_scalar_refusals_say_what_to_do_not_just_what_broke():
    """[codex review, 2026-08-29, P2] AGENTS.md:101-102: "Error messages say
    what went wrong and what to do about it. They do not apologise and they
    do not guess." Both refusal branches were missing the second half --
    fixed here, and locked in so it cannot regress silently."""
    with pytest.raises(ValueError, match="check whether the pinned runtime's MechId type"):
        _mechanism_count_scalar([1, 2])
    with pytest.raises(ValueError, match="check whether the pinned runtime's MechId type"):
        _mechanism_count_scalar("not an int")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(True, id="bare_bool_not_a_plain_int"),
        pytest.param("1", id="bare_numeric_string"),
        pytest.param(b"1", id="bare_bytes"),
        pytest.param(1.0, id="bare_float"),
        pytest.param(None, id="bare_none"),
        pytest.param(256, id="bare_int_one_past_mechid_s_u8_range"),
        pytest.param(-1, id="bare_int_negative"),
    ],
)
def test_mechanism_count_refuses_a_bare_scalar_that_is_not_already_a_plain_int(raw):
    """[codex adversarial challenge round 1, 2026-08-29, P2] The bare-scalar
    branch exists for a decoded value that is ALREADY a plain int in
    MechId's own range (a future client that stops wrapping single-field
    composites) -- not a general-purpose ``int()`` coercion. An earlier
    version of this function fell through to a bare ``int(value)`` here,
    which would have silently accepted a bool, a numeric string, bytes, or
    a truncated float -- the exact blind-coercion-on-ambiguity this guard
    exists to refuse, just relocated to the other branch. [codex
    adversarial challenge round 2, 2026-08-29, P2] added the range cases
    (256, -1): a real MechId is a single byte, so neither is a value a
    genuine decode could ever produce. Every one of these must refuse the
    same as an unrecognized list shape does."""
    with pytest.raises(ValueError, match="neither the confirmed single-element list"):
        _mechanism_count_scalar(raw)


@pytest.mark.parametrize("boundary", [0, 255])
def test_mechanism_count_accepts_a_bare_scalar_at_both_ends_of_mechid_s_range(boundary):
    chain = _chain(storage={("SubtensorModule", "MechanismCountCurrent"): boundary})
    assert chain.mechanism_count(137) == boundary


class _LyingInt(int):
    """[codex adversarial challenge round 3, 2026-08-29, P2]'s own proof
    shape: an ``int`` subclass that reports itself as ``1`` to every
    comparison (so an ``isinstance(x, int)``-plus-range check would have
    accepted it) while a later ``int(x)`` call on it would actually
    resolve to ``256`` -- because ``int.__new__`` was handed 256 at
    construction and only the comparison dunders were overridden to lie.

    ``__ge__``/``__le__`` compute "self <compare> other" AS IF self were
    ``1`` -- i.e. ``1 >= other`` / ``1 <= other`` -- not ``other``'s side
    of the comparison. [codex adversarial challenge round 4, 2026-08-29,
    P2] caught an earlier version of this class with ``__ge__`` backwards
    (``other >= 1`` instead of ``1 >= other``), which made ``0 <= lying``
    evaluate to ``lying.__ge__(0)`` -> ``0 >= 1`` -> ``False`` -- so the
    range check the docstring claimed this class would pass was never
    actually exercised. Fixed, and the range chain is now asserted
    directly below rather than only claimed in prose."""

    def __new__(cls):
        return int.__new__(cls, 256)

    def __eq__(self, other):
        return other == 1

    def __le__(self, other):
        return 1 <= other

    def __ge__(self, other):
        return 1 >= other

    def __hash__(self):
        return hash(1)


def test_lying_int_actually_fools_an_isinstance_plus_range_check():
    """Proves the vulnerability class itself, independent of this
    project's own guard: an ``isinstance(x, int) and not isinstance(x,
    bool) and 0 <= x <= 255`` check -- the shape ``_is_plain_mechid_int``
    used before [codex adversarial challenge round 3] -- says yes for
    ``_LyingInt()``, while ``int()`` on the very same object resolves to a
    completely different value. This is what makes ``type(value) is int``
    a real fix and not a defense against an impossible input."""
    lying = _LyingInt()
    would_pass_the_old_check = (
        isinstance(lying, int) and not isinstance(lying, bool) and 0 <= lying <= 255
    )
    assert would_pass_the_old_check is True
    assert int(lying) == 256


def test_mechanism_count_refuses_an_int_subclass_even_one_that_lies_about_its_value():
    """The exact gap codex demonstrated: ``_LyingInt()`` satisfies
    ``isinstance(x, int)`` and, through its overridden comparisons, an
    ``0 <= x <= 255`` range check too -- yet ``int(x)`` on it resolves to
    256, not the 1 its comparisons claim. ``type(value) is int`` rejects
    it outright, before any comparison is ever evaluated, so this never
    reaches the point of trusting a lying subclass -- in either the list
    or the bare-scalar shape."""
    lying = _LyingInt()
    assert lying == 1  # sanity: this really does lie about its own value
    assert int(lying) == 256

    with pytest.raises(ValueError, match="neither the confirmed single-element list"):
        _mechanism_count_scalar(lying)
    with pytest.raises(ValueError, match="not shaped like the single-element"):
        _mechanism_count_scalar([lying])


# -- the sibling audit: every other int(_plain(...)) site, proven not just asserted --


def test_subnetwork_n_handles_the_bare_scalar_shape_confirmed_live():
    """SubnetworkN's value type is a bare u16 (pallets/subtensor/src/lib.rs:
    2217) -- confirmed live on 2026-08-21/22/23 (256, 16, 107, always a
    plain int, never a list). No guard needed; this locks the expected
    shape in as a regression test."""
    chain = _chain(storage={("SubtensorModule", "SubnetworkN"): 256})
    assert chain.subnetwork_n(137) == 256


def test_subnetwork_n_handles_the_scalecodec_wrapped_scalar_shape_too():
    chain = _chain(storage={("SubtensorModule", "SubnetworkN"): _ScaleValue(107)})
    assert chain.subnetwork_n(137) == 107


def test_subnetwork_n_still_raises_type_error_on_a_list_unlike_mechanism_count():
    """Documents, by contrast, that subnetwork_n deliberately got NO guard:
    SubnetworkN is not MechId-typed, so a list shape here would be a
    genuinely unexplained anomaly worth a loud crash, not a shape this
    method should learn to unwrap. If SubnetworkN's runtime type ever
    changes to something struct-wrapped, this test starts failing and is
    the signal to build it a guard of its own rather than silently
    reusing mechanism_count's."""
    chain = _chain(storage={("SubtensorModule", "SubnetworkN"): [256]})
    with pytest.raises(ChainUnavailable, match="not 'list'"):
        chain.subnetwork_n(137)


def test_seat_hotkeys_int_plain_key_handles_the_bare_u16_uid_shape():
    """Keys' second key component is a bare u16 uid
    (pallets/subtensor/src/lib.rs:2652) -- the ``int(_plain(k))`` call this
    reads through. The VALUE side is exercised separately below (issue
    #536): unlike the uid key, it is never already an SS58 string."""
    chain = _chain(
        maps={
            ("SubtensorModule", "Keys"): [
                (0, [tuple(_HOTKEY_A_BYTES)]),
                (1, [tuple(_HOTKEY_B_BYTES)]),
            ]
        }
    )
    result = chain.seat_hotkeys(137)
    assert result == {0: _HOTKEY_A_ADDRESS, 1: _HOTKEY_B_ADDRESS}


def test_registration_blocks_int_plain_key_and_value_handle_bare_scalars():
    """BlockAtRegistration's value is a bare u64
    (pallets/subtensor/src/lib.rs:2740) and its key is the same bare u16
    uid as Keys -- both ``int(_plain(...))`` calls this reads through."""
    chain = _chain(maps={("SubtensorModule", "BlockAtRegistration"): [(0, 12345), (1, 137)]})
    result = chain.registration_blocks(137)
    assert result == {0: 12345, 1: 137}


# -- issue #536: the str(_plain(...)) family, the sibling audit missed ------
#
# Two real chain addresses this project already trusts as fixtures
# (``noah/deposits/tests/test_encode.py``'s own VECTORS, generated with
# ``scalecodec``/``cyscale`` and pinned) -- used here as the known-real
# (bytes -> SS58 string) pair the encoding correctness is checked against,
# not just "it produced A string."

_HOTKEY_A_ADDRESS = "5FnWMK2TA5cWRXJRNyESbiKcHVMfcSCxadNzEoSSQ771gibF"
_HOTKEY_A_BYTES = bytes.fromhex("a496462f793063e46feef40cec16f884ad75991092f205d50d52af1e920cc245")
_HOTKEY_B_ADDRESS = "5GbwBbBDnvAymE4yf8b3aQBEuYDJJ7xsbTRThYvhb8D9QTXJ"
_HOTKEY_B_BYTES = bytes.fromhex("c8c266c468653658531392cbe69e6186832dfa06beecc7dd6df82e69aacc6567")

#: The exact shape NOAH-TESTNET's live read confirmed for Keys[2] at block
#: 937 (evidence commit 572230d): a length-1 list wrapping a 32-element
#: tuple of plain ints.
_LIVE_ACCOUNT_ID32_SHAPE = [
    (
        232,
        22,
        81,
        44,
        179,
        96,
        74,
        121,
        35,
        200,
        49,
        13,
        14,
        244,
        125,
        57,
        91,
        96,
        143,
        6,
        153,
        76,
        163,
        60,
        26,
        147,
        138,
        187,
        25,
        60,
        3,
        88,
    )
]
_LIVE_ACCOUNT_ID32_ADDRESS = "5HK1bX6v8CY33ZvexLqnwcpNeY2E6QnmGtTAWnThvygPdF3S"


def test_the_old_str_plain_shape_is_what_actually_produced_garbage():
    """``live.py`` before this fix did ``str(_plain(v))`` on a Keys-map
    value -- reproduced here from the pre-fix expression itself against the
    EXACT wire shape NOAH-TESTNET's live read returned (evidence commit
    572230d), not a description of it. The result is the literal
    stringified Python list-of-tuple the live crash report quoted, and
    running it through this project's own address decoder refuses it --
    now on length alone ([codex adversarial challenge, 2026-08-29, P2]'s
    own length guard, added after this same challenge demonstrated
    ``_base58_bytes`` had no upper bound on the text it would decode,
    catches a 140-character string well before the character-alphabet
    check would; see the dedicated test below for the alphabet check on a
    string short enough to reach it)."""
    garbage = str(_plain(_LIVE_ACCOUNT_ID32_SHAPE))
    assert garbage == (
        "[(232, 22, 81, 44, 179, 96, 74, 121, 35, 200, 49, 13, 14, 244, 125, "
        "57, 91, 96, 143, 6, 153, 76, 163, 60, 26, 147, 138, 187, 25, 60, 3, "
        "88)]"
    )
    assert len(garbage) > 64  # confirms this reaches the length guard, not a coincidence
    with pytest.raises(NotAnAddress, match="longer than any genuine address"):
        public_key_of(garbage)


def test_a_short_bracketed_garbage_string_still_fails_on_its_alphabet():
    """The length guard above catches the REAL historical garbage (140
    characters), but the alphabet check this module always had is still
    there and still correct for anything short enough to reach it --
    proven directly rather than assumed once the length guard started
    intercepting the one example this file already had."""
    short_garbage = "[1]"
    assert len(short_garbage) <= 64
    with pytest.raises(NotAnAddress, match=r"outside the address alphabet"):
        public_key_of(short_garbage)


def test_seat_hotkeys_decodes_the_confirmed_live_shape_to_a_real_address():
    """The exact shape from ``572230d``'s evidence, decoded through the
    fixed ``seat_hotkeys`` to the real SS58 address ``cyscale``'s own
    ``ss58_encode`` produces for the identical bytes (verified directly
    against the library at fix time; pinned here as a regression)."""
    chain = _chain(maps={("SubtensorModule", "Keys"): [(2, _LIVE_ACCOUNT_ID32_SHAPE)]})
    assert chain.seat_hotkeys(937) == {2: _LIVE_ACCOUNT_ID32_ADDRESS}


def test_seat_hotkeys_decodes_both_known_real_vectors_correctly():
    """The known-real (bytes -> SS58) cross-check the D-130 gate asks for,
    not just "produced some string": both of ``test_encode.py``'s real
    chain address vectors, run through the confirmed wire shape, come back
    as the exact addresses they started as."""
    chain = _chain(
        maps={
            ("SubtensorModule", "Keys"): [
                (0, [tuple(_HOTKEY_A_BYTES)]),
                (1, [tuple(_HOTKEY_B_BYTES)]),
            ]
        }
    )
    assert chain.seat_hotkeys(137) == {0: _HOTKEY_A_ADDRESS, 1: _HOTKEY_B_ADDRESS}


def test_seat_hotkeys_handles_the_real_scalecodec_double_wrapper_shape_too():
    """Both layers of ``_plain`` exercised, not just the outer one -- the
    same realism check ``test_mechanism_count_unwraps_through_the_real_
    scalecodec_wrapper_shape`` applies to ``MechId``, applied here to
    ``AccountId32``: the outer list, its one element, AND each of the 32
    bytes inside can all arrive as scale-objects with a ``.value``."""
    wrapped = _ScaleValue([_ScaleValue(tuple(_ScaleValue(b) for b in _HOTKEY_A_BYTES))])
    chain = _chain(maps={("SubtensorModule", "Keys"): [(0, wrapped)]})
    assert chain.seat_hotkeys(137) == {0: _HOTKEY_A_ADDRESS}


def test_seat_hotkeys_accepts_a_bare_unwrapped_32_byte_sequence_too():
    """If a future client version stops wrapping single-field composites,
    the same allowance ``_mechanism_count_scalar`` makes for a bare scalar
    applies here to a bare 32-length sequence -- no outer list to unwrap,
    but held to the identical byte checks."""
    chain = _chain(maps={("SubtensorModule", "Keys"): [(0, tuple(_HOTKEY_A_BYTES))]})
    assert chain.seat_hotkeys(137) == {0: _HOTKEY_A_ADDRESS}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="empty_list"),
        pytest.param([tuple(_HOTKEY_A_BYTES), tuple(_HOTKEY_B_BYTES)], id="two_element_list"),
        pytest.param([[tuple(_HOTKEY_A_BYTES)]], id="nested_list"),
        pytest.param([tuple(_HOTKEY_A_BYTES[:-1])], id="thirty_one_bytes_not_thirty_two"),
        pytest.param([tuple(_HOTKEY_A_BYTES) + (0,)], id="thirty_three_bytes_not_thirty_two"),
        pytest.param([("a",) * 32], id="thirty_two_elements_but_strings_not_ints"),
        pytest.param([(True,) * 32], id="thirty_two_elements_but_bools_not_plain_ints"),
        pytest.param([(256,) * 32], id="thirty_two_elements_but_one_past_a_byte_s_range"),
        pytest.param([(-1,) * 32], id="thirty_two_elements_but_negative"),
        pytest.param(42, id="a_bare_int_not_any_confirmed_shape"),
        pytest.param(None, id="bare_none"),
        pytest.param(3.14, id="bare_float"),
    ],
)
def test_account_id32_address_refuses_every_shape_that_is_not_a_confirmed_one(raw):
    """The issue's own warning, same as #532's and #537's: unwrapping is
    safe ONLY because each accepted shape is confirmed real. Anything else
    -- wrong outer length, nesting, wrong element count, wrong element
    type, an out-of-range byte, or a value that is not even list-, tuple-
    or string-shaped -- must refuse loudly rather than be guessed at, or a
    genuinely wrong future decode would be silently accepted the same way
    the three real shapes now correctly are. String inputs get their OWN
    dedicated refusal tests below, because issue #537 gave strings a
    different (checksum-based) refusal message, not this generic one."""
    with pytest.raises(ValueError, match="AccountId32"):
        _account_id32_address(raw)


def test_account_id32_address_refusals_name_the_runtime_source_and_issue():
    """Diagnosable from the message alone, the same discipline
    ``_mechanism_count_scalar``'s refusals were held to."""
    with pytest.raises(ValueError, match=r"check whether the pinned runtime's AccountId32 type"):
        _account_id32_address([1, 2])
    with pytest.raises(ValueError, match=r"check whether the pinned runtime's AccountId32 type"):
        _account_id32_address(None)


def test_account_id32_address_refuses_a_lying_int_subclass_hidden_in_one_byte():
    """[codex adversarial challenge round 3, 2026-08-29, P2] found this gap
    in ``_is_plain_mechid_int``; ``_is_plain_byte`` shares the identical
    ``type(value) is int`` defense, proven here against the same lying
    subclass rather than assumed to share the fix because it shares the
    name."""
    lying = _LyingInt()
    bytes_with_a_lying_one = tuple(_HOTKEY_A_BYTES[:-1]) + (lying,)
    with pytest.raises(ValueError, match="AccountId32"):
        _account_id32_address([bytes_with_a_lying_one])


def test_seat_hotkeys_accepts_a_bare_unwrapped_32_byte_list_too():
    """[codex review, 2026-08-29, P2]: a bare 32-element LIST, not just a
    bare 32-element TUPLE, must be accepted by the same "future client
    stopped wrapping" allowance -- an earlier version of the guard checked
    ``isinstance(value, list)`` before trying the bare-sequence shape, so a
    list of exactly 32 honest bytes was refused (mistaken for a mis-shaped
    single-element wrapper) while the identical tuple was accepted right
    next to it."""
    chain = _chain(maps={("SubtensorModule", "Keys"): [(0, list(_HOTKEY_A_BYTES))]})
    assert chain.seat_hotkeys(137) == {0: _HOTKEY_A_ADDRESS}


# -- issue #537: a THIRD confirmed shape -- already-formatted SS58 strings --
#
# NOAH-TESTNET's very next live run after #536 landed (block 137, evidence
# commit 5aa2695) found SubnetOwner/SubnetOwnerHotkey decoding as plain,
# already-formatted SS58 strings -- not the list-wrapped shape Keys
# produces, even though both share the identical declared T::AccountId
# type. Confirmed from async-substrate-interface's own source (see
# _account_id32_address's docstring): query() decodes through scale_decode
# (interpreted Python), query_map() decodes through
# runtime.runtime_config.batch_decode (a separate, compiled Cython path in
# cyscale) whenever the runtime implements scale-info -- genuinely
# different decoders for the identical declared type, which is why the
# shape tracks the READ METHOD, not the storage item's declared type.
#
# hotkey_owners' Owner read is also a single query() call (like
# SubnetOwner/SubnetOwnerHotkey, unlike Keys' query_map()), so it is
# expected to share this string shape live too -- exercised below.


def test_account_id32_address_accepts_a_genuine_already_formatted_ss58_string():
    """The exact shape NOAH-TESTNET's live read confirmed for
    SubnetOwnerHotkey[2] at block 137 (evidence commit 5aa2695) -- and,
    not a coincidence, the identical real address this file's own
    ``_LIVE_ACCOUNT_ID32_ADDRESS`` decodes Keys[2]'s block-937 bytes to:
    the same hotkey, read through two different client methods, in two
    different wire shapes."""
    assert _account_id32_address(_LIVE_ACCOUNT_ID32_ADDRESS) == _LIVE_ACCOUNT_ID32_ADDRESS


def test_account_id32_address_accepts_both_known_real_vector_strings():
    for address in (_HOTKEY_A_ADDRESS, _HOTKEY_B_ADDRESS):
        assert _account_id32_address(address) == address


def test_account_id32_address_refuses_a_string_that_fails_its_own_checksum():
    """A well-formed-looking but mistyped address -- exactly the case
    ``public_key_of``'s own test suite covers -- must still refuse here,
    named as a checksum failure rather than accepted because it merely
    LOOKS like an address."""
    typo = _HOTKEY_A_ADDRESS[:-1] + ("F" if _HOTKEY_A_ADDRESS[-1] != "F" else "G")
    with pytest.raises(ValueError, match="checksummed SS58 address"):
        _account_id32_address(typo)


def test_account_id32_address_does_not_reintroduce_536_by_accepting_any_string():
    """Issue #537's own hard requirement, proven directly rather than
    trusted: the EXACT garbage string #536's own bug produced (``str()``
    of the mis-decoded Keys[2] list, reproduced byte-for-byte from
    ``test_the_old_str_plain_shape_is_what_actually_produced_garbage``
    above) must still be refused now that strings are a recognized shape
    -- a bare ``isinstance(x, str)`` acceptance would have silently let
    this exact historical bug back in."""
    garbage = str(_plain(_LIVE_ACCOUNT_ID32_SHAPE))
    with pytest.raises(ValueError, match="not a genuine, correctly checksummed SS58 address"):
        _account_id32_address(garbage)


def test_account_id32_address_refuses_a_checksum_valid_string_with_an_invalid_prefix():
    """[codex adversarial challenge, 2026-08-29, P2]'s own crafted proof: a
    string with a first decoded byte of 128 -- outside BOTH the one-byte
    range (below 64) and the real two-byte range (64-127, since a genuine
    two-byte SS58 prefix's first byte always has its top two bits fixed at
    01) -- computed a correct checksum for that exact (invalid-prefix, key)
    pair and was accepted by an earlier version of ``public_key_of``. A
    correct checksum is necessary but not sufficient: the prefix byte it
    commits to must also be one a real encoder could produce."""
    key = bytes(range(32))
    body = bytes([128, 0]) + key
    checksum = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2]
    crafted = _base58_text(body + checksum)
    with pytest.raises(ValueError, match="not a genuine, correctly checksummed SS58 address"):
        _account_id32_address(crafted)


def test_account_id32_address_refuses_an_implausibly_long_string_before_decoding_it():
    """[codex adversarial challenge, 2026-08-29, P2]: ``_base58_bytes`` had
    no length check, so a very long chain-sourced string could force
    disproportionate CPU/memory building one large integer before any
    other check ran. A genuine AccountId32 address is at most 50
    characters (2-byte prefix + 32-byte key + 2-byte checksum, computed);
    a 10,000-character string is refused on length alone."""
    huge = "1" * 10_000
    with pytest.raises(ValueError, match="longer than any genuine address"):
        _account_id32_address(huge)


def test_seat_hotkeys_accepts_bare_bytes_bytearray_and_memoryview_too():
    """[codex adversarial challenge, 2026-08-29, P2]: an earlier version of
    the "bare 32-length sequence" allowance only actually checked
    ``isinstance(value, (list, tuple))``, so a decoder rendering a
    ``[u8; 32]`` field as raw ``bytes`` directly -- at least as plausible a
    representation as a tuple of 32 ints -- was refused even though the
    guard's own docstring promised to accept "a bare 32-length sequence"
    generically. All three variants must now be accepted identically."""
    for wrap in (bytes, bytearray, memoryview):
        chain = _chain(maps={("SubtensorModule", "Keys"): [(0, wrap(_HOTKEY_A_BYTES))]})
        assert chain.seat_hotkeys(137) == {0: _HOTKEY_A_ADDRESS}, wrap


def test_account_id32_address_refuses_a_thirty_three_byte_memoryview():
    """The bytes-family shapes get the same length discipline as list and
    tuple do -- an off-by-one memoryview is not silently truncated or
    padded."""
    with pytest.raises(ValueError, match="AccountId32"):
        _account_id32_address(memoryview(bytes(_HOTKEY_A_BYTES) + b"\x00"))


# -- hotkey_owners: proven an independent bug, not just a propagation -------


def test_hotkey_owners_decodes_the_confirmed_live_shape_a_plain_ss58_string():
    """``hotkey_owners``'s ``Owner`` read goes through a single
    ``self._connection.query(...)`` -- the same client method
    ``SubnetOwner``/``SubnetOwnerHotkey`` use, and issue #537's live
    evidence (block 137, commit 5aa2695) confirmed THAT method decodes
    ``T::AccountId`` as an already-formatted string, not ``Keys``' (read
    via ``query_map``) list-wrapped shape. This is the shape ``Owner``'s
    real reads are actually expected to arrive in -- not the list-wrapped
    one the next test below also proves the shared helper accepts."""
    chain = _chain(storage={("SubtensorModule", "Owner"): _HOTKEY_B_ADDRESS})
    result = chain.hotkey_owners([_HOTKEY_A_ADDRESS], 137)
    assert result == {_HOTKEY_A_ADDRESS: _HOTKEY_B_ADDRESS}


def test_hotkey_owners_also_decodes_the_list_wrapped_shape_the_shared_helper_covers():
    """``Owner``'s value is bare ``T::AccountId`` (pallets/subtensor/src/
    lib.rs:1471-1472), the identical declared type as ``Keys``' -- so even
    though issue #537 showed the declared type does not by itself decide
    the shape, the shared helper still accepts this shape too, defensively,
    in case a future client version routes ``query`` through a
    list-wrapping decoder."""
    chain = _chain(
        block_hash=_HASH,
        storage={("SubtensorModule", "Owner"): [tuple(_HOTKEY_B_BYTES)]},
    )
    result = chain.hotkey_owners([_HOTKEY_A_ADDRESS], 137)
    assert result == {_HOTKEY_A_ADDRESS: _HOTKEY_B_ADDRESS}


def test_hotkey_owners_own_decode_is_independently_broken_pre_fix_not_merely_propagated():
    """Proves, rather than assumes, that ``hotkey_owners`` carries its OWN
    bug and is not simply a victim of ``seat_hotkeys`` handing it a
    malformed hotkey argument.

    The one live crash NOAH-TESTNET actually hit (evidence commit
    572230d) failed while ENCODING the query's own INPUT argument -- the
    garbled hotkey string ``seat_hotkeys`` produced -- before any response
    was ever decoded, so that specific crash is explained by propagation
    alone. This test removes that variable entirely: the hotkey argument
    here is a real, valid SS58 address (``_HOTKEY_A_ADDRESS``, no
    propagated garbage anywhere upstream), and the query still comes back
    with ``Owner``'s own AccountId32-shaped value. The PRE-FIX expression
    (``str(_plain(...))``, reproduced literally, not through the live
    class) turns that honest value into the same unusable stringified
    tuple ``seat_hotkeys`` produced -- proof the decode defect lives
    independently in this call site's own return-value handling, confirmed
    from ``Owner``'s own storage declaration sharing Keys' exact value
    type, not merely inferred from the shared symptom."""
    owner_raw = [tuple(_HOTKEY_B_BYTES)]
    pre_fix_result = str(_plain(owner_raw))
    assert pre_fix_result == str(owner_raw)
    assert len(pre_fix_result) > 64  # long enough to hit the length guard first
    with pytest.raises(NotAnAddress, match="longer than any genuine address"):
        public_key_of(pre_fix_result)

    chain = _chain(storage={("SubtensorModule", "Owner"): owner_raw})
    fixed_result = chain.hotkey_owners([_HOTKEY_A_ADDRESS], 137)
    assert fixed_result == {_HOTKEY_A_ADDRESS: _HOTKEY_B_ADDRESS}


def test_hotkey_owners_refuses_a_malformed_owner_value_by_name():
    chain = _chain(storage={("SubtensorModule", "Owner"): [1, 2]})
    with pytest.raises(ChainUnavailable, match="AccountId32"):
        chain.hotkey_owners([_HOTKEY_A_ADDRESS], 137)


# -- subnet_owner: both AccountId32 reads, direct StorageMap gets -----------


#: The exact strings NOAH-TESTNET's live read confirmed for SubnetOwner[2]
#: and SubnetOwnerHotkey[2] at block 137 (evidence commit 5aa2695) -- both
#: already-formatted SS58 addresses, not list-wrapped byte tuples. Not a
#: coincidence that the hotkey matches _LIVE_ACCOUNT_ID32_ADDRESS above:
#: it is the same real account, read through Keys (block 937, list-wrapped)
#: and through SubnetOwnerHotkey (block 137, plain string) -- two different
#: client methods, two different wire shapes, one real key.
_SUBNET_OWNER_ADDRESS = "5FpotsmJwGGtKpQQT51WACZfy5f7MbGHaLLjapNVb3NwLXoL"
_SUBNET_OWNER_HOTKEY_ADDRESS = "5HK1bX6v8CY33ZvexLqnwcpNeY2E6QnmGtTAWnThvygPdF3S"


def test_subnet_owner_decodes_the_confirmed_live_shape_plain_ss58_strings():
    """The exact values NOAH-TESTNET's live read confirmed at block 137
    (evidence commit 5aa2695): both ``SubnetOwner`` and
    ``SubnetOwnerHotkey`` decode as already-formatted SS58 strings through
    ``_storage`` -> a single ``query()`` -- the real shape for these two
    call sites, corrected from an earlier (wrong) assumption that they
    shared ``Keys``' list-wrapped shape just because they share its
    declared type (issue #537)."""
    chain = _chain(
        storage={
            ("SubtensorModule", "SubnetOwner"): _SUBNET_OWNER_ADDRESS,
            ("SubtensorModule", "SubnetOwnerHotkey"): _SUBNET_OWNER_HOTKEY_ADDRESS,
        }
    )
    assert chain.subnet_owner(137) == (_SUBNET_OWNER_ADDRESS, _SUBNET_OWNER_HOTKEY_ADDRESS)


def test_subnet_owner_also_decodes_the_list_wrapped_shape_the_shared_helper_covers():
    """``SubnetOwner`` and ``SubnetOwnerHotkey`` (pallets/subtensor/src/
    lib.rs:2310-2311,2315-2316) are both bare ``T::AccountId`` too -- the
    shared helper still accepts the list-wrapped shape defensively, proof
    it is correct across every shape flagged across #536 and #537, not
    just the one each call site happens to produce live today."""
    chain = _chain(
        storage={
            ("SubtensorModule", "SubnetOwner"): [tuple(_HOTKEY_A_BYTES)],
            ("SubtensorModule", "SubnetOwnerHotkey"): [tuple(_HOTKEY_B_BYTES)],
        }
    )
    assert chain.subnet_owner(137) == (_HOTKEY_A_ADDRESS, _HOTKEY_B_ADDRESS)


def test_subnet_owner_refuses_a_malformed_value_by_name():
    chain = _chain(
        storage={
            ("SubtensorModule", "SubnetOwner"): [1, 2],
            ("SubtensorModule", "SubnetOwnerHotkey"): [tuple(_HOTKEY_B_BYTES)],
        }
    )
    with pytest.raises(ChainUnavailable, match="AccountId32"):
        chain.subnet_owner(137)


# -- first_emission_block: issue #567 ----------------------------------------
#
# FirstEmissionBlockNumber (pallets/subtensor/src/lib.rs:2207-2208) is a
# bare u64, OptionQuery: None is the honest, expected absence (no start
# call yet), not a defaulted zero -- unlike every other primitive read in
# this file, which is ValueQuery and can never distinguish absence from a
# default.


def test_first_emission_block_is_none_before_any_start_call():
    """The honest OptionQuery absence, passed straight through -- not
    coerced to zero, not refused."""
    chain = _chain(storage={("SubtensorModule", "FirstEmissionBlockNumber"): None})
    assert chain.first_emission_block(137) is None


def test_first_emission_block_reads_the_real_block_once_the_start_call_landed():
    chain = _chain(storage={("SubtensorModule", "FirstEmissionBlockNumber"): 8_123_456})
    assert chain.first_emission_block(9_000_000) == 8_123_456


def test_first_emission_block_unwraps_the_real_scalecodec_wrapper_shape():
    """The realistic full shape: `query()` hands back a scale-object whose
    `.value` is the plain int -- `_plain` unwraps it, matching every other
    bare-primitive read in this file (subnetwork_n, registration_blocks)."""
    chain = _chain(
        storage={("SubtensorModule", "FirstEmissionBlockNumber"): _ScaleValue(8_123_456)}
    )
    assert chain.first_emission_block(9_000_000) == 8_123_456


@pytest.mark.parametrize("boundary", [0, 2**64 - 1])
def test_first_emission_block_accepts_both_ends_of_u64s_real_range(boundary):
    chain = _chain(storage={("SubtensorModule", "FirstEmissionBlockNumber"): boundary})
    assert chain.first_emission_block(9_000_000) == boundary


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(True, id="a_bool_not_a_plain_int"),
        pytest.param("8123456", id="a_numeric_string"),
        pytest.param(8_123_456.0, id="a_float"),
        pytest.param(-1, id="negative"),
        pytest.param(2**64, id="one_past_u64s_own_range"),
        pytest.param([8_123_456], id="a_list_wrapper_this_primitive_has_never_produced"),
    ],
)
def test_first_emission_block_refuses_every_shape_that_is_not_none_or_a_plain_u64(raw):
    """Deliberately NOT a bare int() cast (unlike this file's older
    subnetwork_n/registration_blocks reads): a bool, a numeric string, a
    float, an out-of-range int, or a list wrapper would all silently pass
    a bare int()/float()-adjacent cast, and this value decides every
    miner's no-fault status the moment it is recorded."""
    chain = _chain(storage={("SubtensorModule", "FirstEmissionBlockNumber"): raw})
    with pytest.raises(ChainUnavailable, match="FirstEmissionBlockNumber decoded as"):
        chain.first_emission_block(9_000_000)


def test_first_emission_block_scalar_directly_names_the_bad_shape_in_its_refusal():
    with pytest.raises(ValueError, match=r"decoded as True"):
        _first_emission_block_scalar(True)


# -- published_ark_pin: issue #571 -------------------------------------------
#
# SubnetIdentitiesV3 (pallets/subtensor/src/lib.rs:2792-2795, spec 450) is a
# struct-valued OptionQuery map, not a bare scalar like FirstEmissionBlockNumber
# above -- these mirror noah/deposits/tests/test_ark_readers.py's own
# published_pin coverage on the identical field, through this class's
# different, block-pinned connection protocol.

_ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"


def test_published_ark_pin_is_none_when_no_identity_record_exists():
    """The honest OptionQuery absence -- not a mismatch, and not this
    method's job to say so; see ArkIdentityNotPublished in
    noah.core.chain_adapter for where that distinction is drawn."""
    chain = _chain(storage={("SubtensorModule", "SubnetIdentitiesV3"): None})
    assert chain.published_ark_pin(9_000_000) is None


def test_published_ark_pin_reads_the_real_address_back():
    chain = _chain(storage={("SubtensorModule", "SubnetIdentitiesV3"): {"additional": _ARK}})
    assert chain.published_ark_pin(9_000_000) == _ARK


def test_published_ark_pin_decodes_bytes_from_the_chain():
    chain = _chain(
        storage={("SubtensorModule", "SubnetIdentitiesV3"): {"additional": _ARK.encode()}}
    )
    assert chain.published_ark_pin(9_000_000) == _ARK


def test_published_ark_pin_treats_an_empty_field_as_none():
    """A record with an empty field is None too, not an empty string --
    matching noah.deposits.ark_readers.published_pin's own identical case."""
    chain = _chain(storage={("SubtensorModule", "SubnetIdentitiesV3"): {"additional": "   "}})
    assert chain.published_ark_pin(9_000_000) is None


def test_published_ark_pin_unwraps_the_real_scalecodec_wrapper_shape():
    """The realistic full shape: query() hands back a scale-object whose
    .value is the decoded struct dict -- _plain unwraps it, matching
    first_emission_block's own identical unwrap above."""
    chain = _chain(
        storage={("SubtensorModule", "SubnetIdentitiesV3"): _ScaleValue({"additional": _ARK})}
    )
    assert chain.published_ark_pin(9_000_000) == _ARK


def test_published_ark_pin_reads_a_different_field_when_asked():
    chain = _chain(
        storage={
            ("SubtensorModule", "SubnetIdentitiesV3"): {
                "additional": _ARK,
                "github_repo": "5SomeHotkey",
            }
        }
    )
    assert chain.published_ark_pin(9_000_000, field="github_repo") == "5SomeHotkey"


# NOTE (issue #33/B04 adoption): two drift-guard tests staged here --
# test_ark_pin_field_matches_ark_readers_pin_field_exactly and
# test_ark_identity_pin_scalar_matches_ark_readers_published_pin_on_shared_inputs
# -- cross-checked this class's ported published-identity decode against
# noah.deposits.ark_readers.published_pin/PIN_FIELD, the miner side's own
# equivalent reader. ark_readers.py is miner-side code (neurons/miner.py's
# own preflight), not validator plumbing, and staying out of #33's scope
# (validator/chain-adapter/period/loop/seats/fixture_build/hand-verification
# only) -- it was also explicitly left unadopted by #10 (see the archived
# Noah README's "Not adopted, and why" section). Removed here rather than
# left to fail with ModuleNotFoundError; the remaining tests in this file
# above still cover published_ark_pin/_ark_identity_pin_scalar's own
# behavior on synthetic, static expectations, unaffected by this cut.
