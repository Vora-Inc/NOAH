"""Signing adapters and submission/readback helpers for unsigned deposits.

The caller supplies the signer and chain connection. Independent before/after
position reads compare the observed change with the intended amount. This is a
helper API, not an unattended deposit policy or a substitute for miner consent."""

from dataclasses import dataclass

from ..chain.sources import Refusal
from .build import UnsignedDeposit


class DepositSubmissionRefused(Refusal):
    """A caller supplied something this module refuses to sign or submit."""


class DepositSubmissionFailed(Refusal):
    """The chain itself rejected, or failed to confirm, the submitted extrinsic."""


class BytesConvertingSigner:
    """Wraps any real, Keypair-shaped object so ``.sign(data)`` always
    converts ``data`` to ``bytes`` before calling the wrapped object's own
    ``.sign`` -- see this module's own docstring, "The signing-boundary
    fix, reused in shape."

    Every other attribute (``ss58_address``, ``public_key``, and anything
    else a real ``Keypair`` exposes) is delegated to the wrapped object
    unchanged, so anything expecting a ``Keypair``-shaped object (e.g.
    ``async_substrate_interface``'s own ``create_signed_extrinsic``) sees
    an object indistinguishable from the real one except for this one,
    deliberate difference. A ``bytes`` input is converted too
    (``bytes(data)`` on an object that is already exactly ``bytes``),
    which is a no-op in every way that matters.
    """

    def __init__(self, keypair):
        if not hasattr(keypair, "sign") or not callable(keypair.sign):
            raise DepositSubmissionRefused(
                f"signer must be a Keypair-shaped object exposing a callable .sign(data), "
                f"got {keypair!r}."
            )
        self._keypair = keypair

    def __getattr__(self, name):
        return getattr(self._keypair, name)

    def sign(self, data):
        return self._keypair.sign(bytes(data))


def _call_params(transaction):
    """``SubtensorModule.transfer_stake``'s own call parameters -- see this
    module's own docstring, "``call_params``' own field names are NOT
    independently confirmed..." for what this assumes and why."""
    return {
        "destination_coldkey": transaction.destination_coldkey,
        "hotkey": transaction.hotkey,
        "origin_netuid": transaction.origin_netuid,
        "destination_netuid": transaction.destination_netuid,
        "alpha_amount": transaction.alpha_amount,
    }


@dataclass(frozen=True)
class SubmissionOutcome:
    """The result of one real (or stub) chain submission, plus the
    independent read-back this module performed against it -- never merely
    the receipt's own ``is_success`` flag.
    """

    extrinsic_hash: object
    block_hash: object
    expected_amount_rao: int
    observed_amount_rao: int
    matches: bool

    def to_evidence_dict(self):
        """A plain, JSON-serializable form -- ``noah.deposits.scenarios.
        ScenarioResult.evidence``'s own ``submission`` field is built from
        this, and every value here must survive a JSON round trip."""
        return {
            "extrinsic_hash": None if self.extrinsic_hash is None else str(self.extrinsic_hash),
            "block_hash": None if self.block_hash is None else str(self.block_hash),
            "expected_amount_rao": self.expected_amount_rao,
            "observed_amount_rao": self.observed_amount_rao,
            "matches": self.matches,
        }


def sign_and_submit(
    transaction, *, signer, chain_client, wait_for_inclusion=True, wait_for_finalization=True
):
    """Compose, sign and submit the supplied unsigned transaction through the client.

    The caller supplies the signer. Submission outcome and independent receipt
    verification remain separate responsibilities."""
    if not isinstance(transaction, UnsignedDeposit):
        raise DepositSubmissionRefused(
            f"transaction must be an UnsignedDeposit, got {transaction!r}."
        )
    if isinstance(transaction.alpha_amount, bool) or not isinstance(transaction.alpha_amount, int):
        raise DepositSubmissionRefused(
            f"transaction.alpha_amount is {transaction.alpha_amount!r}, not a decoded integer."
        )
    if transaction.alpha_amount <= 0:
        raise DepositSubmissionRefused(
            f"refusing to submit a non-positive alpha_amount ({transaction.alpha_amount}); "
            f"build_deposit itself never produces one, so a caller reaching this with one has "
            f"a bug upstream. Nothing was composed or signed."
        )

    call = chain_client.compose_call(
        call_module=transaction.pallet,
        call_function=transaction.call,
        call_params=_call_params(transaction),
    )
    wrapped_signer = BytesConvertingSigner(signer)
    signed = chain_client.create_signed_extrinsic(call=call, keypair=wrapped_signer)
    receipt = chain_client.submit_extrinsic(
        signed, wait_for_inclusion=wait_for_inclusion, wait_for_finalization=wait_for_finalization
    )
    if not getattr(receipt, "is_success", False):
        raise DepositSubmissionFailed(
            f"the chain did not report success for this deposit: "
            f"{getattr(receipt, 'error_message', receipt)!r}. Nothing is trusted deposited."
        )
    return receipt


def submit_and_verify_deposit(
    transaction,
    *,
    signer,
    chain_client,
    hotkey,
    coldkey,
    netuid,
    wait_for_inclusion=True,
    wait_for_finalization=True,
):
    """Compare the origin position before submission and at the receipt block.

    Use pinned reads and compare the observed decrease with the intended amount.
    Unrelated same-block position changes can affect this observation; a success flag
    alone is not an independent amount check."""
    holding_before_rao = chain_client.holding_at(
        hotkey=hotkey, coldkey=coldkey, netuid=netuid, block=chain_client.head_block()
    )
    receipt = sign_and_submit(
        transaction,
        signer=signer,
        chain_client=chain_client,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
    )
    block_hash = getattr(receipt, "block_hash", None)
    if block_hash is None:
        raise DepositSubmissionFailed(
            "the submission receipt names no block_hash to read back from -- nothing is "
            "independently confirmed. Refused rather than trusted on the receipt's own say-so."
        )
    holding_after_rao = chain_client.holding_at(
        hotkey=hotkey, coldkey=coldkey, netuid=netuid, block_hash=block_hash
    )
    observed_amount_rao = holding_before_rao - holding_after_rao
    return SubmissionOutcome(
        extrinsic_hash=getattr(receipt, "extrinsic_hash", None),
        block_hash=block_hash,
        expected_amount_rao=transaction.alpha_amount,
        observed_amount_rao=observed_amount_rao,
        matches=observed_amount_rao == transaction.alpha_amount,
    )
