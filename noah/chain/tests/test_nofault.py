"""The no-fault rule: which days nobody is blamed for, and why so few.

A miss costs a participant their streak, so a day the subnet itself broke has
to be separable from a day somebody simply did not show up. This rule has been
written wrong twice, and both wrong versions are pinned here as tests so no
third version quietly reintroduces them.
"""

import pytest

from noah.chain.nofault import EmissionStateNotObserved, no_fault_for
from noah.chain.store import (
    DeclarationChanged,
    EmissionStateChanged,
    NotAnAmount,
    NotAnIdentifier,
    Store,
)

ANCHOR = 1767571200  # a Monday 00:00:00 UTC
DAY = 86_400
MINER = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"


def a_store(tmp_path, name="noah.db", anchor=ANCHOR):
    return Store.create(tmp_path / name, day_anchor_ts=anchor)


def scanned(store, *days):
    for day in days:
        store.mark_day_scanned(day, from_block=1, to_block=2)


def started(store, day=3, block=8_800_000, offset=0):
    return store.record_emission_start(
        block=block, block_timestamp=ANCHOR + day * DAY + offset, block_hash="0xstart"
    )


def not_started(store, day=2, block=8_700_000):
    return store.record_emission_not_started(
        block=block, block_timestamp=ANCHOR + day * DAY, block_hash="0xabsent"
    )


def declare(store, day=5, block=8_900_000, cause="validator outage", at_day=None):
    at_day = day if at_day is None else at_day
    return store.record_no_fault_declaration(
        day=day,
        block=block,
        block_timestamp=ANCHOR + at_day * DAY + 100,
        block_hash="0xdecl",
        cause=cause,
    )


def paid(store, day, amount, uid=140):
    store.record_attributed_pay(
        coldkey=MINER,
        hotkey=HOTKEY,
        uid=uid,
        block_timestamp=ANCHOR + day * DAY + 100,
        amount_rao=amount,
        block=8_875_000 + day,
        block_hash=f"0x{day:x}",
    )


# ---- before the subnet begins, nobody is at fault ----------------------------
#
# This is the one cause nobody can manufacture and everybody can check. It
# happens once in the subnet's life, at the start call, and the chain keeps the
# block it happened at (subtensor pallets/subtensor/src/lib.rs:2207-2208, spec
# 450; was :2107-2108 at spec 447/448, re-verified 2026-08-28, unchanged, set
# once at subnets/subnet.rs:567 (was :563) under a guard against ever being set twice).


def test_a_day_before_the_subnet_began_emitting_is_no_fault(tmp_path):
    store = a_store(tmp_path)
    started(store, day=3)
    assert no_fault_for(store, MINER, 2) is True


def test_a_day_after_the_subnet_began_emitting_is_no_fault_for_nobody(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    assert no_fault_for(store, MINER, 5) is False


def test_the_day_emission_began_in_is_already_an_emitting_day(tmp_path):
    """Emission starting partway through a day makes it an emitting day, not
    a free one. A miner idle for the rest of it is recorded at fault, which is
    the known limitation this rule carries rather than hides.
    """
    store = a_store(tmp_path)
    scanned(store, 3)
    started(store, day=3, offset=100)
    assert no_fault_for(store, MINER, 3) is False


def test_every_day_before_the_start_day_is_no_fault(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 3)
    started(store, day=3)
    assert [no_fault_for(store, MINER, w) for w in (0, 1, 2, 3)] == [True, True, True, False]


def test_a_pre_emission_day_nobody_scanned_is_still_no_fault(tmp_path):
    """A subnet with no start block set receives no emission at all: it is
    filtered out before any is allocated (coinbase/subnet_emissions.rs:22-31).
    So there is no pay to have missed, and requiring the day to have been
    scanned for pay would refuse to answer a question the chain has settled.
    """
    store = a_store(tmp_path)
    started(store, day=3)
    assert no_fault_for(store, MINER, 0) is True  # day 0 never scanned


# ---- the day the switch is thrown ------------------------------------------
#
# A watcher recording the current state produces "not started yet" and then
# "started" within the same day, because that is what happened. An earlier
# version of this store called that pair a contradiction, refused the write,
# and left the start block unrecordable: every later day then refused forever
# while the transition day silently became a free pass for everybody.


def test_the_transition_day_records_both_observations_without_refusing(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    not_started(store, day=5, block=8_800_000)
    started(store, day=5, block=8_800_500)
    assert no_fault_for(store, MINER, 5) is False
    assert no_fault_for(store, MINER, 4) is True


def test_a_start_at_or_before_a_block_it_was_absent_at_is_refused(tmp_path):
    """Compared on blocks, not days. Emission cannot begin at a height where
    it was already observed not to have begun."""
    store = a_store(tmp_path)
    not_started(store, day=5, block=8_800_500)
    with pytest.raises(EmissionStateChanged) as excinfo:
        started(store, day=5, block=8_800_000)
    assert "8800500" in str(excinfo.value).replace(",", "")


def test_the_start_block_is_recorded_once_and_will_not_change(tmp_path):
    store = a_store(tmp_path)
    started(store, day=3, block=8_800_000)
    with pytest.raises(EmissionStateChanged):
        started(store, day=4, block=8_900_000)


def test_recording_the_same_start_twice_is_free(tmp_path):
    store = a_store(tmp_path)
    assert started(store, day=3, block=8_800_000) == 3
    assert started(store, day=3, block=8_800_000) == 3


def test_the_start_supersedes_an_absence_seen_before_it(tmp_path):
    store = a_store(tmp_path)
    not_started(store, day=1)
    started(store, day=3)
    assert store.subnet_was_emitting(2) is False
    assert store.subnet_was_emitting(3) is True


# ---- absence is never invented, but it is not asked for twice ---------------


def test_a_day_earlier_than_a_recorded_absence_needs_no_second_observation(tmp_path):
    """Emission begins once. If it had not begun by day 5 it had not begun in
    day 3, and refusing to say so halts a run over a settled question."""
    store = a_store(tmp_path)
    not_started(store, day=5)
    assert store.subnet_was_emitting(3) is False
    assert no_fault_for(store, MINER, 3) is True


def test_a_day_after_the_last_absence_with_no_start_recorded_refuses(tmp_path):
    """Nobody looked after day 5, so day 6 is genuinely unknown. Guessing it
    was emitting costs somebody their streak; guessing the other way hands
    everybody a free pass."""
    store = a_store(tmp_path)
    scanned(store, 6)
    not_started(store, day=5)
    with pytest.raises(EmissionStateNotObserved):
        no_fault_for(store, MINER, 6)


def test_a_store_that_observed_nothing_at_all_refuses(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    with pytest.raises(EmissionStateNotObserved):
        no_fault_for(store, MINER, 5)


def test_the_absence_day_is_derived_from_the_block_not_handed_in(tmp_path):
    """Every other day in this store comes from a block timestamp, for the
    reason the module says out loud: a guard on the label protects nothing if
    the bucketing happens somewhere else. A caller-supplied day here would be
    an operator-held switch that excuses everybody in a day of their choosing.
    """
    early = a_store(tmp_path, "early.db", anchor=ANCHOR)
    late = a_store(tmp_path, "late.db", anchor=ANCHOR + DAY)
    assert not_started(early, day=5) == 5
    assert not_started(late, day=5) == 4


# ---- the only other route, and it is deliberately loud ----------------------


def test_a_day_covered_by_a_dated_declaration_is_no_fault(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    declare(store, day=5)
    assert no_fault_for(store, MINER, 5) is True


def test_a_declaration_covers_only_the_day_it_names(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5, 6)
    started(store, day=3)
    declare(store, day=5)
    assert no_fault_for(store, MINER, 5) is True
    assert no_fault_for(store, MINER, 6) is False


def test_a_declaration_keeps_its_cause_and_the_block_it_was_made_at(tmp_path):
    store = a_store(tmp_path)
    declare(store, day=5, cause="validator outage")
    assert store.no_fault_declaration(5) == {
        "day": 5,
        "block": 8_900_000,
        "block_hash": "0xdecl",
        "cause": "validator outage",
    }


def test_a_declaration_cannot_be_dated_before_the_day_it_excuses(tmp_path):
    """A declaration names a day and a cause after the fact. One made before
    the day it covers is a blanket forward excuse, which is the quiet lever
    this whole rule exists to prevent."""
    store = a_store(tmp_path)
    with pytest.raises(NotAnAmount) as excinfo:
        declare(store, day=9, at_day=5)
    assert "9" in str(excinfo.value)


def test_a_declaration_may_be_made_in_the_day_it_covers(tmp_path):
    store = a_store(tmp_path)
    assert declare(store, day=5, at_day=5) == 5


def test_redeclaring_a_day_with_a_different_cause_is_refused(tmp_path):
    """It is a standing public record of Noah's own failure. A pay row that
    changes its answer is refused in this store; the record of our own failure
    is not allowed to be softer than that."""
    store = a_store(tmp_path)
    declare(store, day=5, cause="validator outage")
    with pytest.raises(DeclarationChanged) as excinfo:
        declare(store, day=5, cause="something more flattering")
    message = str(excinfo.value)
    assert "validator outage" in message and "something more flattering" in message


def test_redeclaring_a_day_identically_is_free(tmp_path):
    store = a_store(tmp_path)
    declare(store, day=5, cause="validator outage")
    assert declare(store, day=5, cause="validator outage") == 5


# ---- pay settles it, whatever anything else says ----------------------------


def test_a_miner_paid_in_a_day_is_never_no_fault(tmp_path):
    """The fixture rejects no-fault alongside pay outright, so this cannot be
    left to the caller to notice."""
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    declare(store, day=5)
    paid(store, day=5, amount=35_551_889_238)
    assert no_fault_for(store, MINER, 5) is False


def test_one_miner_being_paid_does_not_remove_another_miners_no_fault(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    declare(store, day=5)
    paid(store, day=5, amount=100)
    assert no_fault_for(store, MINER, 5) is False
    assert no_fault_for(store, "5SomebodyIdle", 5) is True


# ---- the two versions of this rule that were wrong --------------------------


def test_a_paid_day_is_at_fault_however_the_wider_network_was_behaving(tmp_path):
    """The second draft keyed no-fault on the network's emission switch, on the
    reasoning that it is root-controlled and so cannot be faked by Noah. The
    switch does not do what that assumed: it zeroes the TAO flowing into a
    subnet and the alpha injected into its pool, while the alpha minted for
    participants comes from the subnet's own issuance schedule and is untouched
    (subtensor pallets/subtensor/src/coinbase/run_coinbase.rs:239, where
    alpha_out never reads the TAO figure). Miners are paid with the switch off,
    so that rule excused everybody in days they were paid normally.

    The case that rule got wrong is a day where the switch was off and
    participants were paid anyway. Pay is the only observable that separates
    the two, so this asserts on the outcome. An earlier version of this test
    asserted on the function's parameter names instead, which a review showed
    would stay green while the rule was rewritten to read the switch through
    the store.
    """
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    paid(store, day=5, amount=1)
    assert no_fault_for(store, MINER, 5) is False


def test_a_day_the_subnet_could_not_have_paid_in_is_excused_without_a_declaration(tmp_path):
    """The other half of the same distinction: the rule turns on whether the
    subnet had begun, not on how much anything was emitting."""
    store = a_store(tmp_path)
    not_started(store, day=5)
    assert no_fault_for(store, MINER, 5) is True


def test_a_subnet_wide_zero_pay_day_is_not_by_itself_no_fault(tmp_path):
    """The first draft made a day no-fault for everyone when the subnet paid
    nothing to anyone. No miner can bring that about, which was the point, but
    Noah's own validator can: publishing an empty result produces exactly that
    state. That is a hidden lever over everybody's pay, held by the one party
    whose whole proposition is not having one.
    """
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    assert no_fault_for(store, MINER, 5) is False
    assert no_fault_for(store, "5AnybodyElse", 5) is False


# ---- nothing here raises an ordinary exception ------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.record_emission_start(block=0, block_timestamp=ANCHOR, block_hash="0xa"),
        lambda s: s.record_emission_start(block=1, block_timestamp=ANCHOR, block_hash=None),
        lambda s: s.record_emission_not_started(block=-1, block_timestamp=ANCHOR, block_hash="0xa"),
        lambda s: s.record_no_fault_declaration(
            day=1, block=1, block_timestamp=ANCHOR + DAY, block_hash="0xa", cause=""
        ),
        lambda s: s.record_no_fault_declaration(
            day="1", block=1, block_timestamp=ANCHOR + DAY, block_hash="0xa", cause="x"
        ),
    ],
    ids=["start-block-zero", "start-hash-null", "absent-block-negative", "empty-cause", "day-str"],
)
def test_bad_input_refuses_rather_than_raising_an_ordinary_exception(tmp_path, call):
    store = a_store(tmp_path)
    with pytest.raises((NotAnAmount, NotAnIdentifier)):
        call(store)


# ---- absence of data, and the shape of the store ----------------------------


def test_an_emitting_day_that_was_never_scanned_refuses(tmp_path):
    """A declaration cannot excuse a day whose pay nobody read. The fixture
    forbids no-fault beside nonzero pay, so the pay figure has to be known
    before the flag can be set, even when a declaration covers the day."""
    from noah.chain.store import DayNotScanned

    store = a_store(tmp_path)
    started(store, day=3)
    declare(store, day=5)
    with pytest.raises(DayNotScanned):
        no_fault_for(store, MINER, 5)


def test_marking_a_day_scanned_does_not_record_its_emission_state(tmp_path):
    """Two different questions about a day, answered from two different
    records. Scanning reads pay; it says nothing about when emission began."""
    store = a_store(tmp_path)
    assert store.subnet_was_emitting(9) is None
    scanned(store, 9)
    assert store.subnet_was_emitting(9) is None


def test_emission_beginning_exactly_on_the_anchor_puts_day_zero_in(tmp_path):
    """Day zero is the one day where an off-by-one has no neighbour to catch
    it."""
    store = a_store(tmp_path)
    scanned(store, 0)
    assert (
        store.record_emission_start(block=8_800_000, block_timestamp=ANCHOR, block_hash="0xa") == 0
    )
    assert store.subnet_was_emitting(0) is True
    assert no_fault_for(store, MINER, 0) is False


@pytest.mark.parametrize("blank", ["   ", "\t", "\n"], ids=["spaces", "tab", "newline"])
def test_a_declaration_with_a_blank_cause_is_refused(tmp_path, blank):
    """The declaration exists so the discretion cannot be exercised quietly. A
    stated cause of nothing at all is exercising it quietly."""
    store = a_store(tmp_path)
    with pytest.raises(NotAnIdentifier):
        declare(store, day=5, cause=blank)


def test_a_declared_cause_comes_back_exactly_as_it_was_written(tmp_path):
    cause = "validator outage: archive gap 8,875,000 to 8,880,000 \u2014 \u6f0f\u6d1e"
    store = a_store(tmp_path)
    declare(store, day=5, cause=cause)
    assert store.no_fault_declaration(5)["cause"] == cause


def test_a_store_with_an_old_but_current_column_refuses_by_version_number(tmp_path):
    """A store whose `identity` table already has TODAY'S column name
    (`day_anchor_ts`) but an old `user_version` pragma -- the ordinary case
    of a store built by a slightly older release that never renamed a
    column, just added or changed something else. `_stored_anchor` reads
    it fine, so this exercises `Store.open`'s own explicit version-number
    check (the comment just above that check, in store.py, names exactly
    this scenario: "a store built by an older version opens happily and
    then fails deep inside a read")."""
    import sqlite3

    from noah.chain.store import SchemaTooOld

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE identity (day_anchor_ts INTEGER NOT NULL)")
        conn.execute("INSERT INTO identity (day_anchor_ts) VALUES (?)", (ANCHOR,))
        conn.execute("PRAGMA user_version = 2")
    conn.close()
    with pytest.raises(SchemaTooOld) as excinfo:
        Store.open(path, day_anchor_ts=ANCHOR)
    assert "2" in str(excinfo.value)


def test_a_genuinely_old_schema_store_refuses_by_name_not_transiently(tmp_path):
    """AS OF D-139 (2026-08-31), TESTNET'S OWN INDEPENDENT REVIEW: the test
    above builds its "old" store using the CURRENT column name
    (`day_anchor_ts`), so it never actually exercises a REAL pre-D-139
    store -- one whose `identity` table predates the day_anchor_ts rename
    entirely and still carries the OLD column, `week_anchor_ts`. Confirmed
    to fail against the pre-fix `_stored_anchor` before this test was
    written correctly: that code caught only `sqlite3.OperationalError`
    whose message contains "no such table" (an empty/brand-new file) and
    treated every OTHER `OperationalError` -- including "no such column:
    day_anchor_ts", which is exactly what this real old table produces --
    as `StoreUnavailable`, a TRANSIENT refusal. A caller that retries
    transient errors would retry FOREVER against a store that can never
    open under any number of retries, because the schema itself is wrong,
    not the moment it was read. This is that real case, and it must refuse
    by name (`SchemaTooOld`), not as something a retry loop would ever
    treat as recoverable."""
    import sqlite3

    from noah.chain.store import SchemaTooOld, StoreUnavailable

    path = tmp_path / "genuinely_old.db"
    conn = sqlite3.connect(path)
    with conn:
        # The real pre-D-139 column name -- not day_anchor_ts.
        conn.execute("CREATE TABLE identity (week_anchor_ts INTEGER NOT NULL)")
        conn.execute("INSERT INTO identity (week_anchor_ts) VALUES (?)", (ANCHOR,))
        conn.execute("PRAGMA user_version = 12")
    conn.close()
    try:
        Store.open(path, day_anchor_ts=ANCHOR)
    except StoreUnavailable:
        pytest.fail(
            "a genuinely old-schema store was reported as StoreUnavailable (transient) -- a "
            "caller that retries transient errors would retry forever against a store that "
            "can never open"
        )
    except SchemaTooOld as exc:
        assert "day_anchor_ts" in str(exc) or "column" in str(exc)
    else:
        pytest.fail("expected SchemaTooOld, nothing was raised")


def test_a_pre_hotkey_column_store_refuses_cleanly_rather_than_crashing(tmp_path):
    """Refuse a schema-13 store before queries require the schema-14 hotkey column.
    An explicit schema refusal is actionable; a later SQL error is not."""
    import sqlite3

    from noah.chain.store import SchemaTooOld

    path = tmp_path / "pre_hotkey.db"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE identity (day_anchor_ts INTEGER NOT NULL)")
        conn.execute("INSERT INTO identity (day_anchor_ts) VALUES (?)", (ANCHOR,))
        # The real schema-13 shape of noah_deposit: every column this
        # version still has, minus `hotkey`.
        conn.execute(
            """
            CREATE TABLE noah_deposit (
                block           INTEGER NOT NULL CHECK (block > 0),
                event_ordinal   INTEGER NOT NULL CHECK (event_ordinal >= 0),
                extrinsic_index INTEGER NOT NULL CHECK (extrinsic_index >= 0),
                coldkey         TEXT    NOT NULL,
                day            INTEGER NOT NULL CHECK (day >= 0),
                amount_rao      INTEGER NOT NULL CHECK (amount_rao >= 0),
                block_hash      TEXT    NOT NULL,
                carried_by      TEXT    NOT NULL CHECK (length(carried_by) > 0),
                PRIMARY KEY (block, event_ordinal)
            )
            """
        )
        conn.execute("PRAGMA user_version = 13")
    conn.close()
    with pytest.raises(SchemaTooOld) as excinfo:
        Store.open(path, day_anchor_ts=ANCHOR)
    assert "13" in str(excinfo.value)


# ---- a day can go wrong more than once -------------------------------------
#
# The owner ruled on 2026-08-20 that the software publishes its own failure as
# a matter of course, with nobody deciding whether it does. Under the earlier
# discretionary version a day carried at most one considered declaration. Under
# this one a day can carry several, because a day can break more than once,
# and refusing the second would jam the reader on precisely the days that went
# worst.


def test_two_separate_failures_in_one_day_are_both_recorded(tmp_path):
    store = a_store(tmp_path)
    store.record_no_fault_declaration(
        day=5,
        block=10,
        block_timestamp=ANCHOR + 5 * DAY + 10,
        block_hash="0xa",
        cause="validator lost its archive",
    )
    store.record_no_fault_declaration(
        day=5,
        block=99,
        block_timestamp=ANCHOR + 5 * DAY + 900,
        block_hash="0xb",
        cause="and then the node ran out of disk",
    )
    causes = [d["cause"] for d in store.no_fault_declarations(5)]
    assert causes == ["validator lost its archive", "and then the node ran out of disk"]


def test_declarations_for_a_day_come_back_in_the_order_they_were_published(tmp_path):
    store = a_store(tmp_path)
    for block in (99, 10, 50):
        store.record_no_fault_declaration(
            day=5,
            block=block,
            block_timestamp=ANCHOR + 5 * DAY + 900,
            block_hash=f"0x{block:x}",
            cause=f"failure at {block}",
        )
    assert [d["block"] for d in store.no_fault_declarations(5)] == [10, 50, 99]


def test_one_declaration_still_cannot_be_rewritten_afterwards(tmp_path):
    """A published account of our own failure is not edited. Recording a second,
    separate failure is not editing the first."""
    store = a_store(tmp_path)
    declare(store, day=5, block=10, cause="validator lost its archive")
    with pytest.raises(DeclarationChanged):
        declare(store, day=5, block=10, cause="something more flattering")


def test_a_day_with_any_declaration_is_still_no_fault(tmp_path):
    store = a_store(tmp_path)
    scanned(store, 5)
    started(store, day=3)
    declare(store, day=5, block=10)
    declare(store, day=5, block=99)
    assert no_fault_for(store, MINER, 5) is True


def test_a_day_with_no_declarations_reads_as_an_empty_list(tmp_path):
    store = a_store(tmp_path)
    assert store.no_fault_declarations(5) == []


def test_a_miner_paid_before_emission_was_recorded_as_started_is_not_no_fault(tmp_path):
    """Restored after a rewrite of this file dropped it and the guard behind it.

    A pay row in a day the store says the subnet had not begun emitting is two
    records disagreeing. Pay wins, because the fixture rejects a no-fault day
    alongside nonzero pay outright, so producing that pair makes the whole
    fixture unreadable rather than one day wrong.
    """
    store = a_store(tmp_path)
    scanned(store, 2)
    not_started(store, day=2)
    paid(store, day=2, amount=100)
    assert no_fault_for(store, MINER, 2) is False


def test_a_pre_emission_day_with_no_pay_row_is_still_no_fault_without_a_scan(tmp_path):
    """The reason the pay check here asks what the store holds rather than what
    the chain did: days before the subnet started have nothing to scan, and
    requiring a scan would refuse to answer a question the chain has settled."""
    store = a_store(tmp_path)
    not_started(store, day=2)
    assert no_fault_for(store, MINER, 2) is True
