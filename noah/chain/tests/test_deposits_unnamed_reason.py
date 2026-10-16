"""Rule B (Drew's decision #338): every qualifying deposit must name its miner
hotkey, and one that does not credits nobody and is reported as unattributed
with a visible reason, for single-seat wallets too.

One test per call shape. The forms that name the hotkey keep their
``origin_hotkey`` and have no reason. Every other shape has no
``origin_hotkey`` and a frozen reason that names the call. The reason text is
pinned character for character because ``Store.record_noah_deposit`` compares
it on a rescan (an unequal reason is ``ObservationChanged``).

The call trees are hand-built in the decoded shape ``extrinsic.value`` has,
not recorded from a real wrapped Ark deposit (none exists); see the module
docstrings of ``test_deposits_batch_all.py`` and
``test_deposits_stake_and_hotkey_transferred.py`` for what is recorded.
"""

import hashlib

import pytest

from noah.chain.deposits import deposits_in_block
from noah.chain.ss58 import public_key_of

BLOCK = 8_900_000
ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
WRONG_VALIDATOR = "5GWcYL2bDsuvZ2w9dcAKSY64nd82wrh6vKqSAD2oKMiTxuZY"
GIVER = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
MINER = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
MINER_RAW = public_key_of(MINER)
MINER_HASH = "0x" + hashlib.blake2b(MINER_RAW, digest_size=32).hexdigest()
PARENT = "0xparent"
ALPHA = 10_000_000_000
XT = 1


class Owns:
    """The depositing coldkey owns the miner hotkey, at every block."""

    def owner_at(self, *, hotkey, block_hash):
        return GIVER


# ---- events ---------------------------------------------------------------


def added(hotkey=VALIDATOR):
    return {
        "ordinal_in_block": 11,
        "extrinsic_idx": XT,
        "module_id": "SubtensorModule",
        "event_id": "StakeAdded",
        "attributes": [ARK, hotkey, 1, ALPHA, 2, 0],
    }


def plain_transferred(hotkey=VALIDATOR):
    return {
        "ordinal_in_block": 10,
        "extrinsic_idx": XT,
        "module_id": "SubtensorModule",
        "event_id": "StakeTransferred",
        "attributes": [GIVER, ARK, hotkey, 2, 2, 1],
    }


def named_transferred(hotkey=VALIDATOR):
    return {
        "ordinal_in_block": 10,
        "extrinsic_idx": XT,
        "module_id": "SubtensorModule",
        "event_id": "StakeAndHotkeyTransferred",
        "attributes": {
            "origin_coldkey": GIVER,
            "destination_coldkey": ARK,
            "origin_hotkey": MINER,
            "destination_hotkey": hotkey,
            "origin_netuid": 2,
            "destination_netuid": 2,
            "amount": 1,
        },
    }


def remarked():
    return {
        "ordinal_in_block": 12,
        "extrinsic_idx": XT,
        "module_id": "System",
        "event_id": "Remarked",
        "attributes": {"sender": GIVER, "hash": MINER_HASH},
    }


PLAIN = [plain_transferred(), added()]
PLAIN_WITH_REMARK = [plain_transferred(), added(), remarked()]
NAMED = [named_transferred(), added()]
NAMED_WITH_REMARK = [named_transferred(), added(), remarked()]


# ---- calls ----------------------------------------------------------------


def call(module, function, args=()):
    return {"call_module": module, "call_function": function, "call_args": list(args)}


def transfer_stake():
    return call(
        "SubtensorModule",
        "transfer_stake",
        [
            {"name": "destination_coldkey", "value": ARK},
            {"name": "hotkey", "value": VALIDATOR},
            {"name": "origin_netuid", "value": 2},
            {"name": "destination_netuid", "value": 2},
            {"name": "alpha_amount", "value": ALPHA},
        ],
    )


def transfer_stake_and_hotkey():
    return call(
        "SubtensorModule",
        "transfer_stake_and_hotkey",
        [
            {"name": "destination_coldkey", "value": ARK},
            {"name": "origin_hotkey", "value": MINER},
            {"name": "destination_hotkey", "value": VALIDATOR},
            {"name": "origin_netuid", "value": 2},
            {"name": "destination_netuid", "value": 2},
            {"name": "alpha_amount", "value": ALPHA},
        ],
    )


def remark():
    return call(
        "System", "remark_with_event", [{"name": "remark", "value": "0x" + MINER_RAW.hex()}]
    )


def batch_all(*legs):
    return call("Utility", "batch_all", [{"name": "calls", "value": list(legs)}])


def proxy(inner):
    return call(
        "Proxy",
        "proxy",
        [
            {"name": "real", "value": GIVER},
            {"name": "force_proxy_type", "value": None},
            {"name": "call", "value": inner},
        ],
    )


def wrapped(module, function, *legs):
    return call(module, function, [{"name": "calls", "value": list(legs)}])


def run(events, top_call, designated=VALIDATOR, extrinsics=True):
    kwargs = {}
    if extrinsics:
        # The extrinsic under test sits at its real index; index 0 is padding.
        record = {"address": GIVER, "call": top_call} if top_call is not None else {}
        kwargs = {
            "extrinsics": [{"address": None, "call": call("Timestamp", "set")}, record],
            "parent_block_hash": PARENT,
            "owner_source": Owns(),
        }
    return deposits_in_block(
        events,
        ark_coldkey=ARK,
        block=BLOCK,
        block_hash="0xabc",
        designated_validator_hotkey=designated,
        **kwargs,
    )


ADVICE_BATCH = (
    "Send with transfer_stake_and_hotkey, or in a top-level Utility.batch_all with a remark "
    "naming your miner hotkey."
)
WHERE = f"extrinsic {XT} of block {BLOCK}"
PLAIN_REASON = (
    f"No miner hotkey named: this was a plain transfer_stake ({WHERE}). A plain transfer_stake "
    f"carries no miner hotkey, so the deposit credits nobody. {ADVICE_BATCH}"
)
NO_REMARK_REASON = (
    f"No miner hotkey named: the batch_all at {WHERE} has no remark_with_event the reader can "
    f"see (a remark inside Proxy.proxy or another wrapper is not read), so the deposit credits "
    f"nobody. {ADVICE_BATCH}"
)


def wrapper_reason(label):
    return (
        f"No miner hotkey named: the transfer was sent inside {label} ({WHERE}), a wrapper the "
        f"reader does not read through, so any remark inside it is not read and the deposit "
        f"credits nobody. Send directly with transfer_stake_and_hotkey, or in a top-level "
        f"Utility.batch_all with the remark. (Proxy.proxy around transfer_stake_and_hotkey is "
        f"also accepted.)"
    )


# ---- the forms that name the miner hotkey stay credited -------------------


@pytest.mark.parametrize(
    "events, top_call",
    [
        pytest.param(NAMED, transfer_stake_and_hotkey(), id="direct transfer_stake_and_hotkey"),
        pytest.param(
            PLAIN_WITH_REMARK,
            batch_all(transfer_stake(), remark()),
            id="top-level batch_all[transfer_stake, remark]",
        ),
        pytest.param(
            NAMED, proxy(transfer_stake_and_hotkey()), id="Proxy.proxy(transfer_stake_and_hotkey)"
        ),
        pytest.param(
            NAMED_WITH_REMARK,
            proxy(batch_all(transfer_stake_and_hotkey(), remark())),
            id="Proxy.proxy(batch_all[transfer_stake_and_hotkey, remark])",
        ),
        pytest.param(
            NAMED_WITH_REMARK,
            batch_all(transfer_stake_and_hotkey(), remark()),
            id="top-level batch_all[transfer_stake_and_hotkey, remark]",
        ),
        pytest.param(
            NAMED,
            wrapped("Utility", "batch", transfer_stake_and_hotkey()),
            id="Utility.batch[transfer_stake_and_hotkey]",
        ),
        pytest.param(
            NAMED,
            wrapped("Multisig", "as_multi", transfer_stake_and_hotkey()),
            id="Multisig(transfer_stake_and_hotkey)",
        ),
    ],
)
def test_a_form_that_names_the_miner_hotkey_is_credited(events, top_call):
    (deposit,) = run(events, top_call)
    assert deposit.origin_hotkey == MINER
    assert deposit.owner_at_deposit == GIVER
    assert deposit.unattributed_reason is None
    assert deposit.amount_rao == ALPHA


# ---- every other shape names nobody ---------------------------------------


def _unnamed(events, top_call):
    (deposit,) = run(events, top_call)
    assert deposit.origin_hotkey is None
    assert deposit.owner_at_deposit is None
    assert deposit.amount_rao == ALPHA  # still recorded: the alpha really moved
    return deposit.unattributed_reason


def test_a_plain_transfer_stake_names_nobody():
    assert _unnamed(PLAIN, transfer_stake()) == PLAIN_REASON


def test_a_batch_all_with_no_remark_names_nobody():
    assert _unnamed(PLAIN, batch_all(transfer_stake())) == NO_REMARK_REASON


def test_a_proxied_plain_transfer_stake_names_nobody():
    reason = _unnamed(PLAIN, proxy(transfer_stake()))
    assert reason == wrapper_reason("Proxy.proxy")


def test_a_remark_inside_a_proxy_is_not_read_so_the_deposit_names_nobody():
    reason = _unnamed(PLAIN_WITH_REMARK, proxy(batch_all(transfer_stake(), remark())))
    assert reason == wrapper_reason("Proxy.proxy")


def test_utility_batch_with_a_remark_names_nobody():
    reason = _unnamed(PLAIN_WITH_REMARK, wrapped("Utility", "batch", transfer_stake(), remark()))
    assert reason == wrapper_reason("Utility.batch")


def test_utility_force_batch_with_a_remark_names_nobody():
    reason = _unnamed(
        PLAIN_WITH_REMARK, wrapped("Utility", "force_batch", transfer_stake(), remark())
    )
    assert reason == wrapper_reason("Utility.force_batch")


def test_as_derivative_names_nobody():
    reason = _unnamed(PLAIN, call("Utility", "as_derivative", [{"name": "call", "value": 1}]))
    assert reason == wrapper_reason("Utility.as_derivative")


def test_a_multisig_names_nobody():
    reason = _unnamed(PLAIN_WITH_REMARK, wrapped("Multisig", "as_multi", batch_all()))
    assert reason == wrapper_reason("Multisig.as_multi")


def test_an_unreadable_top_level_call_names_nobody():
    reason = _unnamed(PLAIN, None)
    assert reason == wrapper_reason("an unreadable top-level call")


def test_a_call_with_no_module_name_is_unreadable_not_a_crash():
    reason = _unnamed(PLAIN, {"call_module": None, "call_function": 7, "call_args": []})
    assert reason == wrapper_reason("an unreadable top-level call")


def test_the_reason_is_the_same_text_every_time_it_is_read():
    first = _unnamed(PLAIN, transfer_stake())
    second = _unnamed(PLAIN, transfer_stake())
    assert first == second


# ---- what rule B does not change ------------------------------------------


WRONG_VALIDATOR_FIXED = (
    f"staked to {VALIDATOR}, not the designated Ark Validator {WRONG_VALIDATOR} "
    f"-- does not qualify for mining credit"
)


def test_an_unnamed_wrong_validator_deposit_says_fixing_the_validator_alone_would_not_help():
    (deposit,) = run(PLAIN, transfer_stake(), designated=WRONG_VALIDATOR)
    assert deposit.origin_hotkey is None
    assert deposit.unattributed_reason == (
        f"{WRONG_VALIDATOR_FIXED}. It also names no miner hotkey, so sending it to the right "
        f"validator alone would not make it credit. {PLAIN_REASON}"
    )


def test_the_wrong_validator_reason_is_deterministic_and_keeps_a_stable_class():
    from noah.chain.store import _reason_class

    (first,) = run(PLAIN, transfer_stake(), designated=WRONG_VALIDATOR)
    (again,) = run(PLAIN, transfer_stake(), designated=WRONG_VALIDATOR)
    (old,) = run(NAMED, transfer_stake_and_hotkey(), designated=WRONG_VALIDATOR)
    assert first.unattributed_reason == again.unattributed_reason
    # An old stored wrong-validator-only reason is the same class as the new one.
    assert old.unattributed_reason == WRONG_VALIDATOR_FIXED
    assert _reason_class(old.unattributed_reason) == _reason_class(first.unattributed_reason)


def test_a_named_deposit_to_the_wrong_validator_keeps_its_origin_and_gets_that_reason():
    (deposit,) = run(NAMED, transfer_stake_and_hotkey(), designated=WRONG_VALIDATOR)
    assert deposit.origin_hotkey == MINER
    assert "not the designated Ark Validator" in deposit.unattributed_reason


def test_without_extrinsics_nothing_can_be_read_so_nothing_is_reasoned():
    (deposit,) = run(PLAIN, None, extrinsics=False)
    assert deposit.origin_hotkey is None
    assert deposit.unattributed_reason is None
