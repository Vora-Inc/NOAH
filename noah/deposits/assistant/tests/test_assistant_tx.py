"""The built transaction, run through the chain fake and the scoring-side reader."""

import pytest

from noah.chain.deposits import deposits_in_block
from noah.chain.sources import Refusal
from noah.deposits.assistant import assistant_tx as tx
from noah.deposits.assistant.reconcile import _OwnerSource, deposits_in
from noah.deposits.availability import deposit_ceiling
from noah.deposits.deposit_submission import BytesConvertingSigner
from noah.deposits.disclosures import REQUIRED_DISCLOSURES, acknowledge

from .fakes import ARK, COLD, MINER, NETUID, OTHER_VAL, VAL, FakeChain, N

ACK = acknowledge(REQUIRED_DISCLOSURES)
CEIL = deposit_ceiling(position_stake=100 * N, position_collateral=0, coldkey_available=100 * N)


def plan(shape, amount=10 * N, validator=VAL, **kw):
    return tx.plan_deposit(
        shape,
        ark_coldkey=ARK,
        miner_hotkey=MINER,
        validator_hotkey=validator,
        netuid=NETUID,
        amount_rao=amount,
        ceiling=CEIL,
        acknowledgment=ACK,
        **kw,
    )


def land(shape, **chain_kw):
    chain = FakeChain(**chain_kw)
    call = tx.compose(chain, plan(shape))
    from .fakes import FakeKeypair

    signed = chain.create_signed_extrinsic(call=call, keypair=BytesConvertingSigner(FakeKeypair()))
    chain.submit_extrinsic(signed)
    found = deposits_in(chain, ark_coldkey=ARK, validator_hotkey=VAL, block=chain.head)
    return chain, found


@pytest.mark.parametrize("shape", [tx.EARNINGS, tx.STAKED_ON_VALIDATOR])
def test_built_transaction_is_a_qualifying_attributed_delegated_deposit(shape):
    _, found = land(shape)
    assert len(found) == 1
    d = found[0]
    assert d.coldkey == COLD
    assert d.origin_hotkey == MINER  # attributed to the miner, not the validator
    assert d.hotkey == VAL  # delegated to the designated validator
    assert d.amount_rao == 10 * N
    assert d.unattributed_reason is None  # the reader's own "qualifies" test
    assert d.carried_by == (
        "StakeAndHotkeyTransferred" if shape == tx.EARNINGS else "StakeTransferred"
    )


def test_wrong_validator_would_not_qualify():
    chain = FakeChain()
    chain.wrong_validator = OTHER_VAL
    from .fakes import FakeKeypair

    call = tx.compose(chain, plan(tx.EARNINGS))
    chain.submit_extrinsic(
        chain.create_signed_extrinsic(call=call, keypair=BytesConvertingSigner(FakeKeypair()))
    )
    [d] = deposits_in(chain, ark_coldkey=ARK, validator_hotkey=VAL, block=chain.head)
    assert d.origin_hotkey == MINER and "does not qualify" in d.unattributed_reason


def test_batch_without_owning_the_miner_hotkey_is_unattributed():
    chain = FakeChain(owners={MINER: OTHER_VAL})  # signer does not own the hotkey
    from .fakes import FakeKeypair

    call = tx.compose(chain, plan(tx.STAKED_ON_VALIDATOR))
    chain.submit_extrinsic(
        chain.create_signed_extrinsic(call=call, keypair=BytesConvertingSigner(FakeKeypair()))
    )
    [d] = deposits_in(chain, ark_coldkey=ARK, validator_hotkey=VAL, block=chain.head)
    assert d.origin_hotkey is None and d.unattributed_reason


def test_composed_call_shapes():
    chain = FakeChain()
    e = tx.compose(chain, plan(tx.EARNINGS))
    assert e["call_function"] == "transfer_stake_and_hotkey"
    assert e["call_params"] == {
        "destination_coldkey": ARK,
        "origin_hotkey": MINER,
        "destination_hotkey": VAL,
        "origin_netuid": NETUID,
        "destination_netuid": NETUID,
        "alpha_amount": 10 * N,
    }
    b = tx.compose(chain, plan(tx.STAKED_ON_VALIDATOR))
    t, r = b["call_params"]["calls"]
    assert (b["call_function"], t["call_function"], r["call_function"]) == (
        "batch_all",
        "transfer_stake",
        "remark_with_event",
    )
    assert t["call_params"]["hotkey"] == VAL and t["call_params"]["destination_coldkey"] == ARK
    assert len(r["call_params"]["remark"]) == 32


def test_validator_is_required_and_distinct_from_miner():
    for bad in (None, "", MINER):
        with pytest.raises(tx.CannotCompose):
            plan(tx.EARNINGS, validator=bad)


def test_amount_over_ceiling_or_nonpositive_is_refused_not_reduced():
    with pytest.raises(tx.CannotCompose):
        plan(tx.EARNINGS, amount=101 * N)
    with pytest.raises(tx.CannotCompose):
        plan(tx.EARNINGS, amount=0)
    with pytest.raises(Refusal):
        plan(tx.STAKED_ON_VALIDATOR, amount=101 * N)


def test_acknowledgment_is_required():
    with pytest.raises(Refusal):
        tx.plan_deposit(
            tx.EARNINGS,
            ark_coldkey=ARK,
            miner_hotkey=MINER,
            validator_hotkey=VAL,
            netuid=NETUID,
            amount_rao=N,
            ceiling=CEIL,
            acknowledgment=True,
        )


def test_owner_source_adapter_reads_owner():
    assert _OwnerSource(FakeChain()).owner_at(hotkey=MINER, block_hash="x") == COLD
    assert deposits_in_block  # reader import is the one scoring uses
