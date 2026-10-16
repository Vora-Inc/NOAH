"""Compose an attributed deposit transaction for the manual assistant.

The selected stake position determines the transfer shape. Where a remark is
required, bind the miner hotkey to the transfer in an atomic batch. This module
builds calls; the session separately obtains consent, opens the wallet and submits."""

from dataclasses import dataclass

from ...chain.sources import Refusal
from ...chain.ss58 import NotAnAddress, public_key_of
from ..build import UnsignedDeposit, build_deposit
from ..disclosures import require_acknowledgment

EARNINGS = "earnings"
STAKED_ON_VALIDATOR = "staked_on_validator"
SHAPES = (EARNINGS, STAKED_ON_VALIDATOR)


class CannotCompose(Refusal):
    """The transaction would not be a correctly attributed, correctly delegated deposit."""


@dataclass(frozen=True)
class UnsignedEarningsDeposit:
    pallet: str
    call: str
    destination_coldkey: str
    origin_hotkey: str
    destination_hotkey: str
    origin_netuid: int
    destination_netuid: int
    alpha_amount: int


@dataclass(frozen=True)
class DepositPlan:
    """Everything needed to compose the call. Holds no key material."""

    shape: str
    miner_hotkey: str
    validator_hotkey: str
    ark_coldkey: str
    netuid: int
    alpha_amount: int
    earnings: UnsignedEarningsDeposit = None
    transfer: UnsignedDeposit = None


def _same(a, b):
    try:
        return public_key_of(a) == public_key_of(b)
    except NotAnAddress as exc:
        raise CannotCompose(f"not a usable chain address: {exc}") from None


def plan_deposit(
    shape,
    *,
    ark_coldkey,
    miner_hotkey,
    validator_hotkey,
    netuid,
    amount_rao,
    ceiling,
    acknowledgment,
):
    """One plan, or a refusal. Fails closed when the validator is not set."""
    if shape not in SHAPES:
        raise CannotCompose(f"unknown transaction shape {shape!r}.")
    if not validator_hotkey:
        raise CannotCompose(
            "The designated Ark Validator hotkey is required. Without it the deposit would not "
            "be delegated correctly. Nothing is built."
        )
    if _same(miner_hotkey, validator_hotkey):
        raise CannotCompose(
            "The miner hotkey and the Ark Validator hotkey are the same address; they are "
            "different roles. Nothing is built."
        )
    require_acknowledgment(acknowledgment)
    if shape == STAKED_ON_VALIDATOR:
        transfer = build_deposit(
            ark_address=ark_coldkey,
            hotkey=validator_hotkey,
            netuid=netuid,
            amount=amount_rao,
            ceiling=ceiling,
            acknowledgment=acknowledgment,
        )
        return DepositPlan(
            shape,
            miner_hotkey,
            validator_hotkey,
            ark_coldkey,
            netuid,
            amount_rao,
            transfer=transfer,
        )

    if isinstance(amount_rao, bool) or not isinstance(amount_rao, int) or amount_rao <= 0:
        raise CannotCompose(f"a deposit of {amount_rao!r} is not a positive whole number of rao.")
    if amount_rao > ceiling.amount:
        raise CannotCompose(
            f"{amount_rao} rao is more than this position can send ({ceiling.amount} rao, bound by "
            f"the {ceiling.bound_by} limit). Nothing is reduced silently."
        )
    earnings = UnsignedEarningsDeposit(
        pallet="SubtensorModule",
        call="transfer_stake_and_hotkey",
        destination_coldkey=ark_coldkey,
        origin_hotkey=miner_hotkey,
        destination_hotkey=validator_hotkey,
        origin_netuid=netuid,
        destination_netuid=netuid,
        alpha_amount=amount_rao,
    )
    return DepositPlan(
        shape, miner_hotkey, validator_hotkey, ark_coldkey, netuid, amount_rao, earnings=earnings
    )


def _transfer_params(t):
    return {
        "destination_coldkey": t.destination_coldkey,
        "hotkey": t.hotkey,
        "origin_netuid": t.origin_netuid,
        "destination_netuid": t.destination_netuid,
        "alpha_amount": t.alpha_amount,
    }


def compose(chain_client, plan):
    """The composed chain call for ``plan``. ``chain_client`` offers ``compose_call``."""
    if plan.shape == EARNINGS:
        e = plan.earnings
        return chain_client.compose_call(
            call_module=e.pallet,
            call_function=e.call,
            call_params={
                "destination_coldkey": e.destination_coldkey,
                "origin_hotkey": e.origin_hotkey,
                "destination_hotkey": e.destination_hotkey,
                "origin_netuid": e.origin_netuid,
                "destination_netuid": e.destination_netuid,
                "alpha_amount": e.alpha_amount,
            },
        )
    t = plan.transfer
    transfer_call = chain_client.compose_call(
        call_module=t.pallet, call_function=t.call, call_params=_transfer_params(t)
    )
    remark_call = chain_client.compose_call(
        call_module="System",
        call_function="remark_with_event",
        call_params={"remark": public_key_of(plan.miner_hotkey)},
    )
    return chain_client.compose_call(
        call_module="Utility",
        call_function="batch_all",
        call_params={"calls": [transfer_call, remark_call]},
    )


def describe(plan):
    if plan.shape == EARNINGS:
        return (
            "one transfer_stake_and_hotkey from your miner hotkey's earnings to the Ark, "
            "re-delegated to the Ark Validator in the same step"
        )
    return (
        "a batch of transfer_stake (stake already on the Ark Validator) plus an on-chain note "
        "naming your miner hotkey"
    )
