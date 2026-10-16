"""Finding deposits into the Ark in one block's events.

**What an Ark deposit is.** The Ark's permanence comes from its coldkey having
no key at all, so a contribution is alpha whose owning coldkey becomes the
Ark's. Staking to the Ark's hotkey is not that: ownership stays with the earner
and they can unstake tomorrow. Our own verification note said so on 2026-08-15
and an earlier version of this reader keyed on the hotkey anyway, which would
have credited permanence to people who kept their money.

**Why one event is not enough.** The transfer events name both parties and carry
a TAO-equivalent amount (move_stake.rs:149-156, and the field comment at
events.rs:766 says so outright). The stake event carries the alpha that actually
arrived but names only the destination (events.rs:16-24). The Ark holds alpha
and a deposit has to be credited to whoever gave it up, so both are needed, and
they are joined by the extrinsic they came from.
"""

import pytest

from noah.chain.deposits import NotADepositEvent, deposits_in_block

ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
ARK_OTHER_PREFIX = "16FKUtgvnWLbT2kVvYJxjFUr7Bo2QNwyFAn2Bas86UinitcB"
GIVER = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
OTHER = "5E9SJDatKpSZL5k3a4cNHroUkHUDMS2NaVrRkbQabVQznNy4"
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
ARK_HOTKEY = "5GWcYL2bDsuvZ2w9dcAKSY64nd82wrh6vKqSAD2oKMiTxuZY"
BLOCK = 8_889_759


def transferred(origin=GIVER, destination=ARK, xt=7, netuid=80, ordinal=169):
    return {
        "ordinal_in_block": ordinal,
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": xt,
        "module_id": "SubtensorModule",
        "event_id": "StakeTransferred",
        "attributes": [origin, destination, HOTKEY, netuid, netuid, 8_000_000_000],
    }


def added(coldkey=ARK, alpha=469_790_773_712, xt=7, netuid=80, ordinal=145):
    return {
        "ordinal_in_block": ordinal,
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": xt,
        "module_id": "SubtensorModule",
        "event_id": "StakeAdded",
        "attributes": [coldkey, HOTKEY, 8_000_000_000, alpha, netuid, 0],
    }


def removed(coldkey=GIVER, alpha=469_790_773_712, xt=7, netuid=80, ordinal=144):
    return {
        "ordinal_in_block": ordinal,
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": xt,
        "module_id": "SubtensorModule",
        "event_id": "StakeRemoved",
        "attributes": [coldkey, HOTKEY, 8_000_000_000, alpha, netuid, 0],
    }


def find(events, ark_coldkey=ARK):
    return deposits_in_block(events, ark_coldkey=ark_coldkey, block=BLOCK, block_hash="0xabc", designated_validator_hotkey=HOTKEY
    )


# ---- what a deposit is ------------------------------------------------------


def test_a_transfer_into_the_ark_coldkey_is_a_deposit():
    got = find([removed(), added(), transferred()])
    assert len(got) == 1
    assert got[0].coldkey == GIVER and got[0].amount_rao == 469_790_773_712
    assert got[0].token_netuid == 80 and got[0].block == BLOCK


def test_the_wallet_credited_is_the_one_that_gave_it_up():
    """The stake event names only where the alpha landed. Who gave it is on the
    transfer event, and crediting the destination would credit the Ark itself."""
    assert find([removed(), added(), transferred()])[0].coldkey == GIVER


def test_the_amount_is_the_alpha_that_arrived_not_the_tao_equivalent():
    """The transfer event's amount is a TAO-equivalent. The Ark holds alpha."""
    got = find([removed(), added(alpha=123_456), transferred()])
    assert got[0].amount_rao == 123_456


def test_staking_to_the_arks_hotkey_is_not_a_deposit():
    """The failure an earlier version of this reader would have had. Ownership
    does not move, so the staker can unstake it tomorrow. Crediting it would
    hand permanent credit to somebody who kept their money."""
    assert find([added(coldkey=GIVER)]) == []


def test_a_transfer_to_somebody_elses_coldkey_is_not_a_deposit():
    assert find([removed(), added(coldkey=OTHER), transferred(destination=OTHER)]) == []


def test_the_ark_is_recognised_written_for_another_network():
    got = find([removed(), added(), transferred()], ark_coldkey=ARK_OTHER_PREFIX)
    assert len(got) == 1 and got[0].coldkey == GIVER


def test_a_block_with_nothing_in_it_yields_nothing():
    assert find([]) == []


def test_unrelated_events_are_ignored():
    noise = {
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": 3,
        "module_id": "Balances",
        "event_id": "Transfer",
        "attributes": [GIVER, OTHER, 5],
    }
    assert find([noise, removed(), added(), transferred()])[0].coldkey == GIVER


# ---- several in one block ---------------------------------------------------


def test_two_deposits_in_one_block_are_both_found():
    events = [
        removed(xt=7),
        added(xt=7),
        transferred(xt=7),
        removed(coldkey=OTHER, xt=14, ordinal=154),
        added(alpha=555, xt=14, ordinal=155),
        transferred(origin=OTHER, xt=14, ordinal=156),
    ]
    got = find(events)
    assert [(d.coldkey, d.amount_rao) for d in got] == [
        (GIVER, 469_790_773_712),
        (OTHER, 555),
    ]


def test_each_deposit_keeps_the_position_of_the_event_that_carried_its_amount():
    events = [
        removed(xt=7),
        added(xt=7),
        transferred(xt=7),
        removed(coldkey=OTHER, xt=14, ordinal=154),
        added(alpha=555, xt=14, ordinal=155),
        transferred(origin=OTHER, xt=14, ordinal=156),
    ]
    got = find(events)
    assert [d.event_ordinal for d in got] == [145, 155]
    assert [d.extrinsic_index for d in got] == [7, 14]


def test_deposits_come_back_in_the_order_they_happened():
    events = [
        removed(coldkey=OTHER, xt=14, ordinal=154),
        added(alpha=555, xt=14, ordinal=155),
        transferred(origin=OTHER, xt=14, ordinal=156),
        removed(xt=7),
        added(xt=7),
        transferred(xt=7),
    ]
    assert [d.event_ordinal for d in find(events)] == [145, 155]


# ---- halves that do not make a whole ----------------------------------------


def test_a_transfer_into_the_ark_with_no_amount_beside_it_is_refused():
    """The transfer says a contribution happened and carries only a
    TAO-equivalent. Without the stake event there is no alpha figure, and
    guessing one would credit the wrong number forever."""
    with pytest.raises(NotADepositEvent, match="no stake event"):
        find([transferred()])


def test_an_amount_into_the_ark_with_no_transfer_beside_it_is_refused():
    """Alpha arrived at the Ark and nothing says who gave it up. Recording it
    against nobody loses the contribution; guessing an owner invents one."""
    with pytest.raises(NotADepositEvent, match="nothing says who"):
        find([added()])


def test_two_transfers_into_the_ark_in_one_extrinsic_are_refused():
    """Which giver goes with which amount is not on the chain, and pairing them
    by order would be a guess about somebody's permanent contribution."""
    events = [
        added(xt=7),
        added(alpha=555, xt=7),
        transferred(xt=7),
        transferred(origin=OTHER, xt=7),
    ]
    with pytest.raises(NotADepositEvent, match="more than one"):
        find(events)


def test_a_transfer_and_an_amount_in_different_extrinsics_do_not_pair():
    with pytest.raises(NotADepositEvent):
        find([added(xt=7), transferred(xt=9)])


# ---- shapes the chain does not send -----------------------------------------


@pytest.mark.parametrize(
    "attributes",
    [[GIVER, ARK, HOTKEY, 80, 80], [GIVER, ARK, HOTKEY, 80, 80, 1, 1], None, "x", {}],
    ids=["five", "seven", "null", "string", "mapping"],
)
def test_a_transfer_without_its_six_fields_is_refused(attributes):
    with pytest.raises(NotADepositEvent, match="6 unnamed fields"):
        find([added(), dict(transferred(), attributes=attributes)])


@pytest.mark.parametrize("bad", [b"x", None, "", "nope"], ids=["bytes", "null", "empty", "prose"])
def test_a_giver_that_is_not_an_address_is_refused(bad):
    with pytest.raises(NotADepositEvent):
        find([added(), transferred(origin=bad)])


@pytest.mark.parametrize(
    "bad",
    [ARK + "\n", " " + ARK, "the ark", "0xdead", "", None, 5],
    ids=["newline", "space", "prose", "hex", "empty", "null", "int"],
)
def test_an_ark_coldkey_that_is_not_an_address_is_refused(bad):
    with pytest.raises(NotADepositEvent):
        find([], ark_coldkey=bad)


@pytest.mark.parametrize("alpha", ["500", 500.0, True, -1], ids=["str", "float", "bool", "neg"])
def test_an_amount_that_is_not_a_decoded_whole_number_is_refused(alpha):
    with pytest.raises(NotADepositEvent, match="alpha"):
        find([added(alpha=alpha), transferred()])


def test_a_deposit_of_nothing_is_refused_rather_than_ignored():
    """The chain cannot announce a stake of nothing: every path that emits this
    event refuses a zero first (stake_utils.rs:941-944, collateral.rs:358-361).
    A zero here is a value that failed to decode."""
    with pytest.raises(NotADepositEvent, match="cannot announce"):
        find([added(alpha=0), transferred()])


@pytest.mark.parametrize("bad", [None, "events", 5, {}], ids=["null", "str", "int", "mapping"])
def test_something_that_is_not_a_list_of_events_is_refused(bad):
    with pytest.raises(NotADepositEvent, match="list of events"):
        find(bad)


def test_an_entry_that_is_not_an_event_record_is_refused():
    with pytest.raises(NotADepositEvent, match="not an event record"):
        find([added(), transferred(), "not an event"])


def test_an_event_with_no_extrinsic_behind_it_is_refused():
    with pytest.raises(NotADepositEvent, match="no extrinsic"):
        find([added(), dict(transferred(), extrinsic_idx=None)])


# ---- guards the rewrite left standing with nothing behind them --------------
#
# These four were covered before this module was rebuilt and were not carried
# across. The guards stayed in the code, so nothing broke and the test count
# barely moved, which is what makes a rewrite the place coverage goes quietly.
#
# Found by diffing what the tests were named, and confirmed by switching each
# guard off and watching the suite stay green. The name diff alone would have
# been wrong in both directions here: most of the tests that vanished were
# renamed rather than lost, and one that looked lost was already covered.


@pytest.mark.parametrize(
    "bad", [None, "", 5, b"x", "   "], ids=["null", "empty", "int", "bytes", "blank"]
)
def test_a_block_hash_that_is_not_usable_text_is_refused(bad):
    """A published figure is traced back through this. Text that no later read
    can match makes the row untraceable rather than wrong, which is worse,
    because nothing about it looks broken."""
    with pytest.raises(NotADepositEvent, match="block hash"):
        deposits_in_block([], ark_coldkey=ARK, block=BLOCK, block_hash=bad, designated_validator_hotkey=HOTKEY)


@pytest.mark.parametrize(
    "bad", ["8889759", 8889759.0, True, -1], ids=["str", "float", "bool", "neg"]
)
def test_a_block_height_that_is_not_a_decoded_whole_number_is_refused(bad):
    with pytest.raises(NotADepositEvent, match="block height"):
        deposits_in_block([], ark_coldkey=ARK, block=bad, block_hash="0xabc", designated_validator_hotkey=HOTKEY)


@pytest.mark.parametrize("netuid", ["80", 80.0, True, -1], ids=["str", "float", "bool", "neg"])
def test_a_subnet_that_is_not_a_decoded_whole_number_is_refused(netuid):
    """The subnet is what makes a contribution in another token convertible
    later without re-reading a chain that will not answer twice."""
    with pytest.raises(NotADepositEvent, match="subnet the alpha arrived on"):
        find([removed(), added(netuid=netuid), transferred()])


def test_a_deposit_carries_the_block_hash_it_was_seen_at():
    """Not decoration. It is the height a published figure walks back to."""
    assert find([removed(), added(), transferred()])[0].block_hash == "0xabc"


def test_a_deposit_carries_the_token_it_arrived_in():
    got = find([removed(), added(netuid=35), transferred(netuid=35)])
    assert got[0].token_netuid == 35


def test_a_deposit_carries_the_hotkey_the_alpha_arrived_on():
    """Added for noah/deposits/live_services.py's verify() reader, which needs
    to confirm not just that the Ark received alpha from a coldkey, but that
    a specific position moved. Kept as the raw SS58 string StakeAdded sent,
    not `_key`'s normalised return -- the same choice already made for
    `coldkey` a few lines above in the source, and for the identical reason:
    a caller comparing this against a string it already holds needs the
    string back, not the public key underneath it."""
    got = find([removed(), added(), transferred()])
    assert got[0].hotkey == HOTKEY


@pytest.mark.parametrize("bad", [b"x", None, "", "nope"], ids=["bytes", "null", "empty", "prose"])
def test_a_hotkey_that_is_not_an_address_is_refused(bad):
    landing = dict(added(), attributes=[ARK, bad, 8_000_000_000, 469_790_773_712, 80, 0])
    with pytest.raises(NotADepositEvent):
        find([landing])


# ---- the refusals written as a fix, which nothing was exercising ------------
#
# These two were added this evening as the fix for a finding where two
# contributions made in one transaction collapsed into a single row and somebody
# lost credit for money nobody can give back. A sweep found that nothing reached
# them, so the fix and the hole it closes had the same coverage, which was none.


def test_two_transfers_into_the_ark_in_one_transaction_are_refused():
    """Which giver goes with which amount is not on the chain. Pairing them by
    the order they appear would be a guess about somebody's permanent
    contribution, so the run stops and a person looks."""
    events = [added(xt=7), transferred(xt=7), transferred(origin=OTHER, xt=7)]
    with pytest.raises(NotADepositEvent, match="more than one transfer"):
        find(events)


def test_two_arrivals_into_the_ark_in_one_transaction_are_refused():
    """The other half of the same pair, and it fires first because arrivals are
    read before transfers in a block's event list."""
    events = [added(xt=7), added(alpha=555, xt=7), transferred(xt=7)]
    with pytest.raises(NotADepositEvent, match="more than one arrival"):
        find(events)


def test_the_same_transaction_in_two_different_blocks_is_two_deposits():
    """Extrinsic numbers restart every block, so the refusals above must be
    scoped to one block and not to a number."""
    first = find([removed(), added(), transferred()])
    assert len(first) == 1
    second = deposits_in_block(
        [removed(), added(), transferred()],
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        block=BLOCK + 1,
        block_hash="0xdef",
    )
    assert len(second) == 1 and second[0].block == BLOCK + 1


# ---------------------------------------------------------------------------
# Two transfer events, two different shapes. Found on the first real day.
# ---------------------------------------------------------------------------


def a_named_transfer(destination, ordinal=140, xt=4, origin=GIVER, amount=577_869_707):
    """The real shape of StakeAndHotkeyTransferred, copied from a recorded block.

    A struct with seven NAMED fields (macros/events.rs:752-769), not the
    six-position tuple StakeTransferred uses (macros/events.rs:293-303). It
    arrives as a mapping and there is no order to index into.

    Recorded at block 8,885,105 on 2026-08-20, where the day held 64 blocks
    carrying one of these.
    """
    return {
        "ordinal_in_block": ordinal,
        "extrinsic_idx": xt,
        "module_id": "SubtensorModule",
        "event_id": "StakeAndHotkeyTransferred",
        "attributes": {
            "origin_coldkey": origin,
            "destination_coldkey": destination,
            "origin_hotkey": HOTKEY,
            "destination_hotkey": ARK_HOTKEY,
            "origin_netuid": 53,
            "destination_netuid": 53,
            "amount": amount,
        },
    }


def test_the_named_transfer_is_read_by_its_field_names():
    """Reading it positionally would work by coincidence and then stop working.

    destination_coldkey sits at index 1 in both events, so a decoder that simply
    accepted seven fields would find the Ark correctly and read the giving hotkey
    where it meant the receiving one, and the destination subnet where it meant
    the amount.
    """
    events = [
        a_named_transfer(ARK, ordinal=140),
        added(coldkey=ARK, alpha=469_790_773_712, xt=4, netuid=53, ordinal=141),
    ]
    found = list(deposits_in_block(events, ark_coldkey=ARK, block=8_885_105, block_hash="0xabc", designated_validator_hotkey=HOTKEY))

    assert len(found) == 1
    assert found[0].coldkey == GIVER, "the wallet that gave the alpha up"
    assert found[0].amount_rao == 469_790_773_712, "the alpha that arrived, not the event's TAO"


def test_a_named_transfer_to_somebody_else_is_not_ours():
    events = [a_named_transfer(OTHER, ordinal=140)]
    assert (
        list(deposits_in_block(events, ark_coldkey=ARK, block=8_885_105, block_hash="0xabc", designated_validator_hotkey=HOTKEY)) == []
    )


def test_a_block_full_of_named_transfers_to_strangers_does_not_stop_the_run():
    """What actually happened: the shape check ran before the destination check,
    so the reader refused on 64 blocks a day whether or not the Ark was in them."""
    events = [a_named_transfer(OTHER, ordinal=n) for n in range(120, 140)]
    assert (
        list(deposits_in_block(events, ark_coldkey=ARK, block=8_885_105, block_hash="0xabc", designated_validator_hotkey=HOTKEY)) == []
    )


def test_the_positional_transfer_is_refused_if_it_arrives_as_a_mapping():
    """Each decoder refuses the other's shape rather than coping with it."""
    wrong = dict(a_named_transfer(ARK), event_id="StakeTransferred")
    with pytest.raises(NotADepositEvent):
        list(deposits_in_block([wrong], ark_coldkey=ARK, block=1, block_hash="0xabc", designated_validator_hotkey=HOTKEY))


def test_the_named_transfer_is_refused_if_it_arrives_as_a_list():
    wrong = {
        "ordinal_in_block": 3,
        "extrinsic_idx": 4,
        "module_id": "SubtensorModule",
        "event_id": "StakeAndHotkeyTransferred",
        "attributes": [GIVER, ARK, HOTKEY, 80, 80, 8_000_000_000],
    }
    with pytest.raises(NotADepositEvent):
        list(deposits_in_block([wrong], ark_coldkey=ARK, block=1, block_hash="0xabc", designated_validator_hotkey=HOTKEY))


def test_a_named_transfer_missing_a_field_is_refused_by_the_missing_name():
    broken = a_named_transfer(ARK)
    del broken["attributes"]["destination_coldkey"]
    with pytest.raises(NotADepositEvent) as refused:
        list(deposits_in_block([broken], ark_coldkey=ARK, block=1, block_hash="0xabc", designated_validator_hotkey=HOTKEY))
    assert "destination_coldkey" in str(refused.value)


# ---- issue #282 item 4/4a, #302: the designated-validator-hotkey check ----


def test_a_deposit_staked_to_the_designated_validator_qualifies():
    """Regression guardrail: unchanged from today's behavior when the
    deposit's own position matches the configured designated validator."""
    got = find([removed(), added(), transferred()])
    assert len(got) == 1
    assert got[0].unattributed_reason is None


def test_a_deposit_staked_to_the_wrong_validator_is_recorded_but_does_not_qualify():
    """A real deposit -- the alpha genuinely moved to the Ark -- staked to
    a hotkey OTHER than the configured designated validator is still a
    real, visible row (never dropped), but its `unattributed_reason` names
    the mismatch and it does not qualify for mining credit."""
    other_validator = "5E9SJDatKpSZL5k3a4cNHroUkHUDMS2NaVrRkbQabVQznNy4"
    assert other_validator != HOTKEY
    got = deposits_in_block(
        [removed(), added(), transferred()],
        ark_coldkey=ARK,
        block=BLOCK,
        block_hash="0xabc",
        designated_validator_hotkey=other_validator,
    )
    assert len(got) == 1
    deposit = got[0]
    assert deposit.coldkey == GIVER and deposit.amount_rao == 469_790_773_712
    assert deposit.unattributed_reason is not None
    assert HOTKEY in deposit.unattributed_reason
    assert other_validator in deposit.unattributed_reason
    assert "does not qualify for mining credit" in deposit.unattributed_reason
