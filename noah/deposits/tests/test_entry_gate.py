"""Rule 1, "entry before score" (Drew's direct ruling, #7 issuecomment-5623324414,
quoted in issue #16's own body): "no score and no weight for a miner until its
100 NOAH entry deposit is confirmed in the Ark ... including a miner that takes
over a UID previously used by someone else."

**Why this lives here, against ``Store``, rather than against a fixture.**
The daily scoring engine
(``noah.scoring.engine``) is, by its own docstring, "driven entirely by
fixture data -- no chain reads inside it", and its ``MinerInput.entryDepositRao``
field is populated by the upstream chain-to-fixture pipeline; the golden
fixtures under ``fixtures/shared/`` show plainly that an ESTABLISHED miner
carries ``entryDepositRao: "0"`` on every ordinary day (the field is a
one-time, registration-day-scoped credit, not a running "has ever deposited"
flag), so a gate placed inside ``score_miner`` on that field alone would
misread every established miner in the existing golden set as never having
deposited and break the whole fixture suite. This module instead enforces
Rule 1 against real, observed chain deposits recorded in ``Store`` via
``cumulative_noah_deposits_for_hotkey`` for each coldkey/hotkey seat. The current
``ChainAdapter.read()`` calls ``exclude_unconfirmed_entries`` before
``build_fixture`` emits ``MinerInput`` rows. ``Store.first_activity_day``
alone would not enforce the gate, because pay can count as activity for a
seat without a confirmed entry deposit.

Exercised end to end through ``Store`` (record → read → gate), the real
unit integration point, without constructing a whole period -- including
the UID-takeover scenario named in the issue: a new coldkey/hotkey takes
over a UID a different coldkey previously held, and must be judged entirely
on its OWN deposit history, never the prior occupant's.
"""

import inspect
import os

import pytest

import noah.deposits.entry_gate as entry_gate
from noah.chain.store import Store
from noah.deposits.entry_gate import (
    EntryDepositNotConfirmed,
    confirm_entry_before_score,
    exclude_unconfirmed_entries,
)

ENTRY_DEPOSIT_RAO_P020 = 100 * 1_000_000_000  # Historical fixture amount.
ANCHOR = 1_800_000_000


def a_store(tmp_path, name="entry_gate.db"):
    return Store.create(os.path.join(tmp_path, name), day_anchor_ts=ANCHOR)


def _pay(store, coldkey, hotkey, uid, day, amount_rao, *, block):
    store.mark_day_scanned(day, block, block)
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=hotkey,
        uid=uid,
        block_timestamp=store.day_anchor_ts + day * 86_400 + 1,
        amount_rao=amount_rao,
        block=block,
        block_hash=f"0x{block:x}",
    )


def _deposit(store, coldkey, day, amount_rao, *, block, event_ordinal=0, hotkey=None):
    store.mark_day_scanned(day, block, block)
    store.record_noah_deposit(
        coldkey=coldkey,
        block_timestamp=store.day_anchor_ts + day * 86_400 + 1,
        amount_rao=amount_rao,
        block=block,
        event_ordinal=event_ordinal,
        extrinsic_index=event_ordinal,
        block_hash=f"0xdep{block:x}",
        carried_by="StakeAdded",
        hotkey=hotkey,
    )


class TestConfirmEntryBeforeScore:
    def test_refuses_a_coldkey_with_no_deposit_at_all(self, tmp_path):
        store = a_store(tmp_path)
        _pay(store, "5NeverDeposited", "5Hot", 3, day=1, amount_rao=10_000, block=10)
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store,
                "5NeverDeposited",
                "5Hot",
                as_of_day=1,
            )

    def test_refuses_when_the_only_deposit_is_wrong_validator(self, tmp_path):
        """Issue #282 item 4/4a, #302: a real deposit that landed on the
        wrong validator (recorded exactly as ``noah.chain.deposits.
        deposits_in_block`` now records one -- a real, non-dropped row,
        ``origin_hotkey`` correctly naming the miner, ``unattributed_
        reason`` naming the actual vs. designated validator) is a genuine
        Ark receipt but never a qualifying one. ``store.
        cumulative_noah_deposits_for_hotkey`` -- what this gate is built
        on -- must exclude it, so a miner whose ONLY deposit is this shape
        does not qualify to begin scoring, even though the alpha is real
        and even though the miner is confidently known."""
        store = a_store(tmp_path)
        store.mark_day_scanned(1, 10, 10)
        store.record_noah_deposit(
            coldkey="5WrongValidator",
            block_timestamp=store.day_anchor_ts + 1 * 86_400 + 1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=10,
            event_ordinal=0,
            extrinsic_index=0,
            block_hash="0xdep10",
            carried_by="StakeTransferred",
            hotkey="5ActualValidatorStakedTo",
            origin_hotkey="5Hot",
            owner_at_deposit="5WrongValidator",
            unattributed_reason=(
                "staked to 5ActualValidatorStakedTo, not the designated Ark Validator "
                "5DesignatedValidator -- does not qualify for mining credit"
            ),
        )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store,
                "5WrongValidator",
                "5Hot",
                as_of_day=1,
            )

    def test_refuses_short_of_the_ruled_figure(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store, "5Short", day=1, amount_rao=ENTRY_DEPOSIT_RAO_P020 - 1, block=10, hotkey="5Hot"
        )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, "5Short", "5Hot", as_of_day=1
            )

    def test_confirms_once_the_100_noah_figure_is_reached(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store, "5Confirmed", day=1, amount_rao=ENTRY_DEPOSIT_RAO_P020, block=10, hotkey="5Hot"
        )
        assert (
            confirm_entry_before_score(
                store, "5Confirmed", "5Hot", as_of_day=1
            )
            == ENTRY_DEPOSIT_RAO_P020
        )

    def test_confirms_only_on_or_after_the_day_the_deposit_landed(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store, "5Later", day=5, amount_rao=ENTRY_DEPOSIT_RAO_P020, block=10, hotkey="5Hot"
        )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, "5Later", "5Hot", as_of_day=4
            )
        assert (
            confirm_entry_before_score(
                store, "5Later", "5Hot", as_of_day=5
            )
            == ENTRY_DEPOSIT_RAO_P020
        )

    def test_several_partial_deposits_accumulate_to_the_figure(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store,
            "5Partial",
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020 // 2,
            block=10,
            hotkey="5Hot",
        )
        _deposit(
            store,
            "5Partial",
            day=2,
            amount_rao=ENTRY_DEPOSIT_RAO_P020 // 2,
            block=20,
            hotkey="5Hot",
        )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, "5Partial", "5Hot", as_of_day=1
            )
        assert (
            confirm_entry_before_score(
                store, "5Partial", "5Hot", as_of_day=2
            )
            == ENTRY_DEPOSIT_RAO_P020
        )

    def test_a_deposit_after_as_of_day_does_not_count_yet(self, tmp_path):
        """Confirms the cutoff is real: a deposit landing AFTER the day being
        judged must not be pulled backward to satisfy an earlier day."""
        store = a_store(tmp_path)
        _deposit(
            store,
            "5FutureDeposit",
            day=10,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=10,
            hotkey="5Hot",
        )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store,
                "5FutureDeposit",
                "5Hot",
                as_of_day=9,
            )


class TestConfirmEntryBeforeScorePerSeat:
    """#112, D-138 reopened: one coldkey can now legitimately hold up to 32
    live seats, each funded by its own independent entry deposit. Rule 1
    must confirm each SEAT on its own real deposit, never on a sibling
    seat's -- the adversarial property this reopening exists to prove,
    exercised directly against Store here rather than only through
    exclude_unconfirmed_entries below."""

    def test_a_well_funded_seat_never_vouches_for_an_unfunded_sibling_seat(self, tmp_path):
        store = a_store(tmp_path)
        coldkey = "5MultiSeat"
        funded_hotkey = "5FundedHot"
        unfunded_hotkey = "5UnfundedHot"
        _deposit(
            store,
            coldkey,
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=10,
            hotkey=funded_hotkey,
        )

        assert (
            confirm_entry_before_score(
                store,
                coldkey,
                funded_hotkey,
                as_of_day=1,
            )
            == ENTRY_DEPOSIT_RAO_P020
        )
        # The SAME coldkey's own aggregate deposit reaches the figure, but
        # the unfunded seat made none of it itself -- it must not be
        # confirmed by its sibling's real deposit.
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store,
                coldkey,
                unfunded_hotkey,
                as_of_day=1,
            )

    def test_two_seats_of_the_same_coldkey_each_below_the_figure_do_not_combine(self, tmp_path):
        """Neither seat alone reaches the ruled figure, and their COMBINED
        coldkey-wide total would -- confirming this refuses both proves the
        check is genuinely per-seat, not a coldkey-wide sum with an extra
        hotkey argument along for the ride."""
        store = a_store(tmp_path)
        coldkey = "5TwoHalves"
        hotkey_a = "5HalfA"
        hotkey_b = "5HalfB"
        _deposit(
            store,
            coldkey,
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020 - 1,
            block=10,
            hotkey=hotkey_a,
        )
        _deposit(
            store,
            coldkey,
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020 - 1,
            block=11,
            hotkey=hotkey_b,
        )
        assert store.cumulative_noah_deposits(coldkey, 1) >= ENTRY_DEPOSIT_RAO_P020

        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, coldkey, hotkey_a, as_of_day=1
            )
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, coldkey, hotkey_b, as_of_day=1
            )

    def test_a_deposit_recorded_with_no_hotkey_confirms_no_seat(self, tmp_path):
        """A deposit recorded before #112 reopened D-138 (hotkey=None) is
        not guessed onto any seat -- direct, no heuristic. See
        Store.noah_deposits_for_hotkey's own docstring."""
        store = a_store(tmp_path)
        _deposit(store, "5Legacy", day=1, amount_rao=ENTRY_DEPOSIT_RAO_P020, block=10)
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store, "5Legacy", "5SomeHotkey", as_of_day=1
            )


class TestConfirmEntryBeforeScoreHasNoCallerArgumentPath:
    """Ticket #112 (2026-09-19), corrected same day: an earlier revision
    made ``entry_deposit_rao`` a keyword argument here, defaulting to
    ``ENTRY_DEPOSIT_RAO_P020``. #16/Drew rejected that -- a keyword
    argument with a safe default is still an argument a caller could pass
    the wrong value through. Proven here the same two ways
    ``noah.deposits.d143``'s own
    ``TestEntryDepositFigureHasNoCallerArgumentPath`` proves it: no keyword
    exists, and the real ``noah.config.profile.entry_deposit_rao()`` is
    what actually gets called and drives the comparison."""

    def test_neither_function_accepts_an_entry_deposit_rao_argument(self):
        assert set(inspect.signature(confirm_entry_before_score).parameters) == {
            "store",
            "coldkey",
            "hotkey",
            "as_of_day",
        }
        assert set(inspect.signature(exclude_unconfirmed_entries).parameters) == {
            "store",
            "seats",
            "as_of_day",
        }

    def test_confirm_entry_before_score_calls_the_real_profile_function(
        self, tmp_path, monkeypatch
    ):
        ten_noah = 10_000_000_000
        calls = []

        def fake_entry_deposit_rao():
            calls.append(1)
            return ten_noah

        monkeypatch.setattr(entry_gate, "entry_deposit_rao", fake_entry_deposit_rao)

        store = a_store(tmp_path)
        _deposit(store, "5TenNoah", day=1, amount_rao=ten_noah, block=10, hotkey="5TenNoahHot")
        observed = confirm_entry_before_score(store, "5TenNoah", "5TenNoahHot", as_of_day=1)
        assert observed == ten_noah
        assert len(calls) == 1

        nine_noah = 9_000_000_000
        _deposit(store, "5NineNoah", day=1, amount_rao=nine_noah, block=11, hotkey="5NineNoahHot")
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(store, "5NineNoah", "5NineNoahHot", as_of_day=1)
        assert len(calls) == 2

    def test_exclude_unconfirmed_entries_calls_the_real_profile_function(
        self, tmp_path, monkeypatch
    ):
        ten_noah = 10_000_000_000
        calls = []

        def fake_entry_deposit_rao():
            calls.append(1)
            return ten_noah

        monkeypatch.setattr(entry_gate, "entry_deposit_rao", fake_entry_deposit_rao)

        store = a_store(tmp_path)
        _deposit(store, "5TenNoah", day=1, amount_rao=ten_noah, block=10, hotkey="5TenNoahHot")
        seats = {"5TenNoah": {"5TenNoahHot": 0}}

        kept, dropped = exclude_unconfirmed_entries(store, seats, as_of_day=1)

        assert kept == {"5TenNoah": {"5TenNoahHot": 0}}
        assert dropped == []
        assert len(calls) == 1


class TestUidTakeover:
    """The issue's own named scenario: a new coldkey/hotkey takes over a UID
    a different coldkey previously held. Rule 1 must judge the new occupant
    entirely on its own deposit history -- never inherit the prior
    occupant's confirmed entry deposit through the shared UID."""

    def test_new_occupant_of_a_uid_is_not_confirmed_by_the_old_occupants_deposit(self, tmp_path):
        store = a_store(tmp_path)
        uid = 7

        # C1 registers, deposits its 100 NOAH via its own hotkey, earns pay
        # at uid 7 through day 5.
        _deposit(
            store,
            "5OriginalHolder",
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=5,
            hotkey="5OriginalHotkey",
        )
        for day in range(1, 6):
            _pay(
                store,
                "5OriginalHolder",
                "5OriginalHotkey",
                uid,
                day,
                1_000_000,
                block=day * 100 + 10,
            )

        # C1's hotkey is deregistered; a genuinely different coldkey/hotkey,
        # C2, takes over the SAME uid starting day 6, and earns real pay --
        # but has made no deposit of its own.
        for day in range(6, 9):
            _pay(store, "5NewOccupant", "5NewHotkey", uid, day, 2_000_000, block=day * 100 + 10)

        # The original holder remains confirmed, on the days it actually
        # earned that confirmation.
        assert (
            confirm_entry_before_score(
                store,
                "5OriginalHolder",
                "5OriginalHotkey",
                as_of_day=5,
            )
            == ENTRY_DEPOSIT_RAO_P020
        )

        # The new occupant of the same uid is NOT confirmed: it inherits
        # nothing from the coldkey that used to hold this seat, and nothing
        # from the shared uid either.
        with pytest.raises(EntryDepositNotConfirmed):
            confirm_entry_before_score(
                store,
                "5NewOccupant",
                "5NewHotkey",
                as_of_day=8,
            )

        # Once C2 makes its own real 100-NOAH deposit via its own hotkey, it
        # becomes confirmed from that day forward, same as any other seat.
        _deposit(
            store,
            "5NewOccupant",
            day=8,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=850,
            hotkey="5NewHotkey",
        )
        assert (
            confirm_entry_before_score(
                store,
                "5NewOccupant",
                "5NewHotkey",
                as_of_day=8,
            )
            == ENTRY_DEPOSIT_RAO_P020
        )


class TestExcludeUnconfirmedEntries:
    """The whole-``seats``-map filter (Codex finding F1, 2026-09-13): the
    isolated ``confirm_entry_before_score`` gate existed with no caller
    that actually removed an unconfirmed coldkey from what gets scored --
    a real check, unit-tested, never wired into the path that matters. This
    is the seats-filtering counterpart of
    ``noah.chain.duplicate_coldkeys.exclude_duplicate_coldkeys``, built the
    same shape on purpose so ``noah.core.chain_adapter.ChainAdapter.read()``
    can wire both the same way: ``(kept, dropped) = exclude_unconfirmed_
    entries(store, seats, as_of_day=day)``.
    """

    def test_a_coldkey_with_no_confirmed_deposit_is_excluded_entirely(self, tmp_path):
        store = a_store(tmp_path)
        _pay(store, "5NeverDeposited", "5NeverHot", 3, day=1, amount_rao=10_000, block=10)
        seats = {"5NeverDeposited": {"5NeverHot": 3}}

        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=1
        )

        assert kept == {}
        assert dropped == [{"coldkey": "5NeverDeposited", "hotkey": "5NeverHot", "uid": 3}]

    def test_a_confirmed_coldkey_passes_through_unchanged(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store, "5Confirmed", day=1, amount_rao=ENTRY_DEPOSIT_RAO_P020, block=10,
            hotkey="5ConfirmedHot",
        )
        seats = {"5Confirmed": {"5ConfirmedHot": 0}}

        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=1
        )

        assert kept == {"5Confirmed": {"5ConfirmedHot": 0}}
        assert dropped == []

    def test_a_mix_of_confirmed_and_unconfirmed_coldkeys(self, tmp_path):
        store = a_store(tmp_path)
        _deposit(
            store, "5Confirmed", day=1, amount_rao=ENTRY_DEPOSIT_RAO_P020, block=10,
            hotkey="5ConfirmedHot",
        )
        seats = {
            "5Confirmed": {"5ConfirmedHot": 0},
            "5NeverDeposited": {"5NeverHot": 1},
        }

        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=1
        )

        assert kept == {"5Confirmed": {"5ConfirmedHot": 0}}
        assert {d["coldkey"] for d in dropped} == {"5NeverDeposited"}

    def test_empty_seats_map(self, tmp_path):
        store = a_store(tmp_path)
        kept, dropped = exclude_unconfirmed_entries(
            store, {}, as_of_day=0
        )
        assert kept == {}
        assert dropped == []

    def test_a_multi_seat_coldkey_keeps_only_its_own_confirmed_seat(self, tmp_path):
        """#112, D-138 reopened: the coldkey-level all-or-nothing behavior
        is gone. A coldkey holding two live seats, only one of which has
        made its own real deposit, keeps that ONE seat and drops only the
        other -- never the whole coldkey either way."""
        store = a_store(tmp_path)
        coldkey = "5MultiSeat"
        funded_hotkey = "5FundedHot"
        unfunded_hotkey = "5UnfundedHot"
        _deposit(
            store,
            coldkey,
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=10,
            hotkey=funded_hotkey,
        )
        seats = {coldkey: {funded_hotkey: 0, unfunded_hotkey: 1}}

        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=1
        )

        assert kept == {coldkey: {funded_hotkey: 0}}
        assert dropped == [{"coldkey": coldkey, "hotkey": unfunded_hotkey, "uid": 1}]

    def test_uid_takeover_the_new_occupant_of_a_shared_uid_is_excluded_from_the_seats_map(
        self, tmp_path
    ):
        """The same UID-takeover scenario as TestUidTakeover above, but
        exercised through the seats-map filter this ticket actually wires
        into ChainAdapter.read() -- the new occupant of a UID a different
        coldkey previously held must be excluded from `kept`, never
        inheriting the prior occupant's confirmation through the shared
        UID, and the ORIGINAL holder (a different coldkey, unaffected)
        stays in `kept`."""
        store = a_store(tmp_path)
        uid = 7
        _deposit(
            store,
            "5OriginalHolder",
            day=1,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=5,
            hotkey="5OriginalHotkey",
        )
        for day in range(1, 6):
            _pay(
                store,
                "5OriginalHolder",
                "5OriginalHotkey",
                uid,
                day,
                1_000_000,
                block=day * 100 + 10,
            )
        for day in range(6, 9):
            _pay(store, "5NewOccupant", "5NewHotkey", uid, day, 2_000_000, block=day * 100 + 10)

        # The live seats map at day 8: the ORIGINAL coldkey no longer holds
        # this uid (it was reassigned); only the NEW occupant does now.
        seats = {"5NewOccupant": {"5NewHotkey": uid}}
        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=8
        )

        assert kept == {}
        assert dropped == [{"coldkey": "5NewOccupant", "hotkey": "5NewHotkey", "uid": uid}]

        # Once the new occupant makes its own real deposit, it is kept.
        _deposit(
            store,
            "5NewOccupant",
            day=8,
            amount_rao=ENTRY_DEPOSIT_RAO_P020,
            block=850,
            hotkey="5NewHotkey",
        )
        kept, dropped = exclude_unconfirmed_entries(
            store, seats, as_of_day=8
        )
        assert kept == seats
        assert dropped == []
