"""``confirm_owner_entitled_at_block``, against a FAKE chain reader only.

No real ``async_substrate_interface`` construction and no network connection
anywhere in this file -- ``FakeSource`` below answers a canned map of
``(hotkey, block_hash) -> raw Owner value`` exactly the way a real connection
would answer for one specific historical query, and nothing else.

The whole point under test is the historical pin itself (issue #112, comment
5751446658): a hotkey's owner can change over time, and only who owned it AT
THE DEPOSIT'S OWN BLOCK may have their deposit honestly assign credit to it.
``FakeSource`` therefore answers DIFFERENT owners for the SAME hotkey at
different block hashes, so a version of this check that quietly read "the
fake's current owner" instead of the one named by the block hash it was
actually given would be caught immediately.
"""

import pytest

from noah.deposits.owner_entitlement import (
    OwnerNotEntitled,
    OwnerReadUnreadable,
    confirm_owner_entitled_at_block,
)

HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
OWNER_AT_BLOCK_A = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
OWNER_AT_BLOCK_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
BLOCK_A = "0xaaaa"
BLOCK_B = "0xbbbb"


class FakeSource:
    """Answers ``owner_at`` from a canned ``{(hotkey, block_hash): raw}`` map.

    A lookup miss raises, the same as a real connection asked about a block
    or hotkey it was never told to answer for -- there is no silent
    "current" fallback here for a version of this check to accidentally
    read.
    """

    def __init__(self, answers):
        self._answers = answers
        self.asked = []

    def owner_at(self, *, hotkey, block_hash):
        self.asked.append((hotkey, block_hash))
        key = (hotkey, block_hash)
        if key not in self._answers:
            raise AssertionError(f"FakeSource was never told the answer for {key}")
        return self._answers[key]


def test_reads_the_exact_block_named_not_a_current_value(tmp_path=None):
    """The central proof this function exists for: the SAME hotkey has TWO
    different real owners at two different block hashes, and each is
    confirmed against its OWN block, never the other one."""
    source = FakeSource(
        {
            (HOTKEY, BLOCK_A): OWNER_AT_BLOCK_A,
            (HOTKEY, BLOCK_B): OWNER_AT_BLOCK_B,
        }
    )
    assert confirm_owner_entitled_at_block(
        source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_A, block_hash=BLOCK_A
    )
    assert confirm_owner_entitled_at_block(
        source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_B, block_hash=BLOCK_B
    )
    assert source.asked == [(HOTKEY, BLOCK_A), (HOTKEY, BLOCK_B)]


def test_refuses_when_the_owner_at_that_block_does_not_match():
    """Block A's real owner is OWNER_AT_BLOCK_A. Asking whether
    OWNER_AT_BLOCK_B was entitled at block A -- a wallet that only ever
    owned this hotkey at block B -- must refuse, using block A's own
    answer, not block B's."""
    source = FakeSource(
        {
            (HOTKEY, BLOCK_A): OWNER_AT_BLOCK_A,
            (HOTKEY, BLOCK_B): OWNER_AT_BLOCK_B,
        }
    )
    with pytest.raises(OwnerNotEntitled) as excinfo:
        confirm_owner_entitled_at_block(
            source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_B, block_hash=BLOCK_A
        )
    message = str(excinfo.value)
    assert OWNER_AT_BLOCK_A in message
    assert OWNER_AT_BLOCK_B in message


def test_succeeds_when_the_owner_at_that_block_does_match():
    source = FakeSource({(HOTKEY, BLOCK_A): OWNER_AT_BLOCK_A})
    assert (
        confirm_owner_entitled_at_block(
            source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_A, block_hash=BLOCK_A
        )
        is True
    )


def test_an_unreadable_owner_value_refuses_rather_than_guesses():
    """A malformed decode (not one of the three confirmed AccountId32 shapes,
    or a string that fails a real SS58 checksum) must not be silently
    treated as "no match" -- it is a distinct failure: the read itself could
    not be trusted at all."""
    source = FakeSource({(HOTKEY, BLOCK_A): "not-a-real-address"})
    with pytest.raises(OwnerReadUnreadable):
        confirm_owner_entitled_at_block(
            source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_A, block_hash=BLOCK_A
        )


def test_a_source_failure_is_reported_as_unreadable_not_swallowed():
    class ExplodingSource:
        def owner_at(self, *, hotkey, block_hash):
            raise RuntimeError("connection dropped")

    with pytest.raises(OwnerReadUnreadable):
        confirm_owner_entitled_at_block(
            ExplodingSource(), hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_A, block_hash=BLOCK_A
        )


def test_a_missing_block_hash_refuses_before_any_read():
    source = FakeSource({})
    with pytest.raises(OwnerReadUnreadable):
        confirm_owner_entitled_at_block(
            source, hotkey=HOTKEY, coldkey=OWNER_AT_BLOCK_A, block_hash=None
        )
    assert source.asked == []
