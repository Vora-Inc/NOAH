"""Comparing chain addresses by what they are, not by how they are written.

The Ark's address is configuration. If it is compared as text, every ordinary
way of writing the same key down produces a mismatch, and a mismatch here is
not an error: it is every deposit ever made silently failing to be a deposit.

The same public key has a different text form on every network prefix, picks up
a trailing newline from a configuration file, and may arrive from a decoder as
hex rather than as an address. All four are the same key and none of them are
the same string.
"""

import hashlib

import pytest

from noah.chain.ss58 import NotAnAddress, _base58_text, address_of, public_key_of

# The all-zeros account: its public key is thirty-two zero bytes, which is what
# makes it the value four maps return when they hold no record.
ZERO = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"
# Read from a live archive on 2026-08-20.
REAL = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
REAL_SAME_KEY_PREFIX_ZERO = "16FKUtgvnWLbT2kVvYJxjFUr7Bo2QNwyFAn2Bas86UinitcB"


def test_the_all_zeros_account_decodes_to_thirty_two_zero_bytes():
    assert public_key_of(ZERO) == bytes(32)


def test_a_well_known_key_decodes_to_its_published_public_key():
    """Alice, whose public key is documented and stable, so this pins the
    decoding against something outside this repository."""
    alice = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    assert public_key_of(alice).hex() == (
        "d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d"
    )


def test_the_same_key_on_two_networks_is_the_same_key():
    """The finding this module exists for. Written for one network the address
    starts with a five, for another with a one, and they are one key. Comparing
    the text says they are different and every deposit disappears."""
    assert public_key_of(REAL) == public_key_of(REAL_SAME_KEY_PREFIX_ZERO)
    assert REAL != REAL_SAME_KEY_PREFIX_ZERO


@pytest.mark.parametrize(
    "bad",
    [
        "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr\n",
        " 5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr",
        "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYX0",
        "0xe818d0abd237faf1e3060b549c992cffa4c8ecf3d3a07e43e85fdc9ad1182c69",
        "the ark",
        "",
        "   ",
    ],
    ids=["trailing-newline", "leading-space", "bad-checksum", "hex", "prose", "empty", "blank"],
)
def test_anything_that_is_not_an_address_is_refused(bad):
    """A trailing newline out of a configuration file is the likeliest of these
    and the hardest to see."""
    with pytest.raises(NotAnAddress):
        public_key_of(bad)


@pytest.mark.parametrize("bad", [None, 5, b"bytes", ["5HK2"]], ids=["null", "int", "bytes", "list"])
def test_something_that_is_not_text_is_refused(bad):
    with pytest.raises(NotAnAddress):
        public_key_of(bad)


def test_a_typo_that_stays_base58_is_caught_by_the_checksum():
    """Base58 leaves out the characters that look alike, but a wrong character
    inside the alphabet still reads as a valid address until the checksum is
    tested."""
    typo = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXq"
    with pytest.raises(NotAnAddress, match="checksum"):
        public_key_of(typo)


def test_an_address_of_the_wrong_length_is_refused():
    with pytest.raises(NotAnAddress):
        public_key_of("5HK2LZRrvj581Vjyx")


def test_decoding_is_stable():
    assert public_key_of(REAL) == public_key_of(REAL)


def test_an_address_too_short_says_so_rather_than_failing_its_checksum():
    """Redundant in the sense that a later check would also refuse this, but
    the messages differ and the earlier one is the useful diagnosis: too short
    tells an operator the value was truncated, a checksum failure sends them
    looking for a typo."""
    with pytest.raises(NotAnAddress, match="too few to be an address"):
        public_key_of("5HK2LZ")


def test_an_address_carrying_the_wrong_size_of_key_says_which_size():
    with pytest.raises(NotAnAddress, match="byte key"):
        public_key_of("5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXrABCDE")


# -- issue #537's adversarial challenge: two gaps in public_key_of itself ---


def test_a_checksum_valid_string_with_an_invalid_two_byte_prefix_is_refused():
    """[codex adversarial challenge, 2026-08-29, P2] crafted a string whose
    decoded first byte is 128 -- above 64, so the old code took the
    two-byte-prefix branch, but a REAL two-byte SS58 prefix's first byte is
    always 64-127 (the encoding fixes its top two bits at 01). Computed a
    correct checksum for that exact (invalid-prefix, key) body and had it
    accepted. A checksum proves the bytes were not corrupted in transit; it
    does not prove the prefix byte they commit to is one a genuine encoder
    could ever have produced."""
    key = bytes(range(32))
    body = bytes([128, 0]) + key
    checksum = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2]
    crafted = _base58_text(body + checksum)
    with pytest.raises(NotAnAddress, match="no genuine SS58 encoder ever produces"):
        public_key_of(crafted)


@pytest.mark.parametrize("first_byte", [64, 100, 127])
def test_a_genuine_two_byte_prefix_range_is_still_accepted(first_byte):
    """The fix narrows what counts as a valid two-byte prefix; it must not
    narrow it to nothing. 64, 100 and 127 are real values inside the range
    a genuine two-byte SS58 prefix's first byte can take."""
    key = bytes(range(32))
    body = bytes([first_byte, 0]) + key
    checksum = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2]
    crafted = _base58_text(body + checksum)
    assert public_key_of(crafted) == key


def test_an_implausibly_long_address_is_refused_before_being_decoded():
    """[codex adversarial challenge, 2026-08-29, P2]: ``_base58_bytes`` built
    an arbitrarily large integer with no length check before anything else
    validated the input -- reachable from chain-sourced text this project's
    own trust model already treats as untrustworthy by default. A genuine
    AccountId32 address is at most 50 characters; a 10,000-character string
    is refused on length alone, before the expensive decode runs."""
    with pytest.raises(NotAnAddress, match="longer than any genuine address"):
        public_key_of("1" * 10_000)


def test_the_real_addresses_this_module_already_trusts_stay_under_the_length_bound():
    """The length guard must not be so tight it rejects real addresses --
    checked directly against every real address vector this test file and
    ``test_encode.py`` already use, not just asserted to be generous."""
    for address in (
        ZERO,
        REAL,
        REAL_SAME_KEY_PREFIX_ZERO,
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "5FnWMK2TA5cWRXJRNyESbiKcHVMfcSCxadNzEoSSQ771gibF",
        "5GbwBbBDnvAymE4yf8b3aQBEuYDJJ7xsbTRThYvhb8D9QTXJ",
    ):
        public_key_of(address)  # must not raise on length


# -- address_of: the reverse direction, needed by issue #536 ----------------
#
# noah/chain/live.py's four AccountId32 reads decode a real public key off
# the chain and need it back as a real address -- the direction this module
# never needed until now. Network 42 (generic Substrate/Bittensor) is the
# only prefix this project writes; see address_of's own docstring for why
# that is a deliberate choice and not the same "any prefix" leniency
# public_key_of applies on the way in.


def test_address_of_reproduces_a_well_known_key_s_published_address():
    """Alice again, the same fixture test_a_well_known_key_decodes_to_its_
    published_public_key pins on the way in -- round-tripped the other
    direction, against a public key documented outside this repository
    rather than generated by this module itself."""
    alice = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    alice_key = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
    assert address_of(alice_key) == alice


def test_address_of_is_the_exact_inverse_of_public_key_of():
    assert address_of(public_key_of(REAL)) == REAL


def test_address_of_matches_the_real_chain_address_vectors_encode_py_pins():
    """The known-real (bytes -> SS58 string) cross-check: two real chain
    addresses this project already trusts as fixtures
    (``noah/deposits/tests/test_encode.py``'s own VECTORS, generated with
    ``scalecodec``/``cyscale`` and pinned), decoded to bytes and re-encoded
    here, must reproduce the exact original text."""
    vectors = [
        (
            "5FnWMK2TA5cWRXJRNyESbiKcHVMfcSCxadNzEoSSQ771gibF",
            "a496462f793063e46feef40cec16f884ad75991092f205d50d52af1e920cc245",
        ),
        (
            "5GbwBbBDnvAymE4yf8b3aQBEuYDJJ7xsbTRThYvhb8D9QTXJ",
            "c8c266c468653658531392cbe69e6186832dfa06beecc7dd6df82e69aacc6567",
        ),
    ]
    for address, hex_key in vectors:
        assert address_of(bytes.fromhex(hex_key)) == address


def test_address_of_writes_the_all_zeros_account_the_same_way_it_reads():
    assert address_of(bytes(32)) == ZERO


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(b"too short", id="too_few_bytes"),
        pytest.param(b"x" * 33, id="one_byte_too_many"),
        pytest.param("not bytes at all", id="a_string_not_bytes"),
        pytest.param(None, id="none"),
        pytest.param(list(range(32)), id="a_list_not_bytes"),
    ],
)
def test_address_of_refuses_anything_that_is_not_exactly_32_bytes(bad):
    with pytest.raises(NotAnAddress):
        address_of(bad)
