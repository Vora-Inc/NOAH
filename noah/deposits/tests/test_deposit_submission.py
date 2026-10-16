"""B05 Phase 3: offline tests for signing, submitting, and independently
reading back a computed D-143 deposit -- entirely against a fake
substrate-interface-shaped chain client. No real network connection, no
real Keypair, and no chain read happens anywhere in this file.
"""

import pytest

from noah.deposits.build import UnsignedDeposit
from noah.deposits.deposit_submission import (
    BytesConvertingSigner,
    DepositSubmissionFailed,
    DepositSubmissionRefused,
    sign_and_submit,
    submit_and_verify_deposit,
)

HOTKEY, COLDKEY, ARK, NETUID = "5Hotkey", "5Coldkey", "5ArkAddress", 42


def a_transaction(amount_rao=30_000):
    return UnsignedDeposit(
        pallet="SubtensorModule",
        call="transfer_stake",
        destination_coldkey=ARK,
        hotkey=HOTKEY,
        origin_netuid=NETUID,
        destination_netuid=NETUID,
        alpha_amount=amount_rao,
    )


class FakeKeypair:
    """Records the exact type of payload ``.sign`` actually received --
    never a real Keypair, never a real signature."""

    ss58_address = "5FakeSigner"

    def __init__(self):
        self.received = []

    def sign(self, data):
        self.received.append(data)
        if not isinstance(data, bytes):
            raise TypeError(f"a real Keypair.sign would reject {type(data).__name__}")
        return b"\x00" * 64


class FakeReceipt:
    def __init__(
        self,
        *,
        is_success=True,
        error_message=None,
        extrinsic_hash="0xhash",
        block_hash="0xblock",
    ):
        self.is_success = is_success
        self.error_message = error_message
        self.extrinsic_hash = extrinsic_hash
        self.block_hash = block_hash


class FakeSubstrateClient:
    """A minimal, in-memory chain: submitting a real ``transfer_stake``
    call actually moves ``amount`` rao out of ``holdings[hotkey]``, so a
    test can independently prove the read-back reflects reality (and can
    deliberately desync the two to prove a mismatch is caught).
    """

    def __init__(self, *, holdings, receipt_factory=None, head=1):
        self.holdings = dict(holdings)
        self._receipt_factory = receipt_factory or (lambda: FakeReceipt())
        self._head = head
        self.composed_calls = []
        self.signed_with = []
        self.submitted = []
        self.head_block_calls = 0

    def head_block(self):
        """This stub's own ``head_block`` is intentionally parameterless at
        this call-site's level (the caller asks "what is the head" once) --
        the audited hang risk is in how a REAL ``head_block()`` resolves
        that underneath (fixed in ``live_chain_source.py`` to name an
        explicit finalized-head hash rather than ask the raw connection for
        "latest"), not in this higher-level interface itself. Counted below
        so a regression that calls this more than once per operation (a
        moving-target head, rather than one read threaded through) is
        caught."""
        self.head_block_calls += 1
        return self._head

    def holding_at(self, *, hotkey, coldkey, netuid, block=None, block_hash=None):
        assert (block is None) != (block_hash is None)
        return self.holdings[hotkey]

    def compose_call(self, *, call_module, call_function, call_params):
        call = {
            "call_module": call_module,
            "call_function": call_function,
            "call_params": call_params,
        }
        self.composed_calls.append(call)
        return call

    def create_signed_extrinsic(self, *, call, keypair):
        # A real client's own signing call -- proves the wrapped signer, not
        # the raw one, is what is actually used.
        keypair.sign(bytearray(b"some-scale-encoded-payload"))
        self.signed_with.append(keypair)
        return {"call": call, "signed": True}

    def submit_extrinsic(self, signed, *, wait_for_inclusion, wait_for_finalization):
        self.submitted.append((signed, wait_for_inclusion, wait_for_finalization))
        receipt = self._receipt_factory()
        if getattr(receipt, "is_success", False):
            amount = signed["call"]["call_params"]["alpha_amount"]
            hotkey = signed["call"]["call_params"]["hotkey"]
            self.holdings[hotkey] -= amount
        return receipt


class TestBytesConvertingSigner:
    def test_a_bytearray_is_converted_to_bytes_before_signing(self):
        fake = FakeKeypair()
        signer = BytesConvertingSigner(fake)
        signer.sign(bytearray(b"payload"))
        assert fake.received == [b"payload"]
        assert isinstance(fake.received[0], bytes)

    def test_a_bytes_input_is_unaffected(self):
        fake = FakeKeypair()
        signer = BytesConvertingSigner(fake)
        signer.sign(b"payload")
        assert fake.received == [b"payload"]

    def test_other_attributes_delegate_to_the_wrapped_keypair(self):
        fake = FakeKeypair()
        signer = BytesConvertingSigner(fake)
        assert signer.ss58_address == "5FakeSigner"

    def test_refuses_something_with_no_callable_sign(self):
        with pytest.raises(DepositSubmissionRefused):
            BytesConvertingSigner(object())


class TestSignAndSubmit:
    def test_composes_signs_and_submits_a_real_shaped_call(self):
        client = FakeSubstrateClient(holdings={HOTKEY: 100_000})
        keypair = FakeKeypair()
        receipt = sign_and_submit(a_transaction(30_000), signer=keypair, chain_client=client)
        assert receipt.is_success
        assert client.composed_calls == [
            {
                "call_module": "SubtensorModule",
                "call_function": "transfer_stake",
                "call_params": {
                    "destination_coldkey": ARK,
                    "hotkey": HOTKEY,
                    "origin_netuid": NETUID,
                    "destination_netuid": NETUID,
                    "alpha_amount": 30_000,
                },
            }
        ]
        # The wrapped signer, not the raw FakeKeypair, was handed to the client.
        assert isinstance(client.signed_with[0], BytesConvertingSigner)
        # The client's own signing call used a bytearray -- proving the
        # wrapper, not the raw Keypair, converted it before it ever reached
        # the real .sign.
        assert isinstance(keypair.received[0], bytes)

    def test_raises_when_the_chain_reports_failure(self):
        client = FakeSubstrateClient(
            holdings={HOTKEY: 100_000},
            receipt_factory=lambda: FakeReceipt(is_success=False, error_message="nope"),
        )
        with pytest.raises(DepositSubmissionFailed):
            sign_and_submit(a_transaction(), signer=FakeKeypair(), chain_client=client)

    def test_refuses_a_non_positive_amount(self):
        bad = UnsignedDeposit(
            pallet="SubtensorModule",
            call="transfer_stake",
            destination_coldkey=ARK,
            hotkey=HOTKEY,
            origin_netuid=NETUID,
            destination_netuid=NETUID,
            alpha_amount=0,
        )
        client = FakeSubstrateClient(holdings={HOTKEY: 100_000})
        with pytest.raises(DepositSubmissionRefused):
            sign_and_submit(bad, signer=FakeKeypair(), chain_client=client)

    def test_refuses_something_that_is_not_an_unsigned_deposit(self):
        client = FakeSubstrateClient(holdings={HOTKEY: 100_000})
        with pytest.raises(DepositSubmissionRefused):
            sign_and_submit({"not": "a transaction"}, signer=FakeKeypair(), chain_client=client)


class TestSubmitAndVerifyDeposit:
    def test_a_successful_submission_with_matching_readback(self):
        client = FakeSubstrateClient(holdings={HOTKEY: 100_000})
        outcome = submit_and_verify_deposit(
            a_transaction(30_000),
            signer=FakeKeypair(),
            chain_client=client,
            hotkey=HOTKEY,
            coldkey=COLDKEY,
            netuid=NETUID,
        )
        assert outcome.matches is True
        assert outcome.expected_amount_rao == 30_000
        assert outcome.observed_amount_rao == 30_000
        assert outcome.extrinsic_hash == "0xhash"
        assert outcome.block_hash == "0xblock"
        # Independently confirmed against the client's own real state, not
        # merely trusted from the receipt's is_success flag.
        assert client.holdings[HOTKEY] == 70_000
        # Regression guard: the "before" read is pinned to exactly one head
        # read (never a fresh, possibly-moved "latest" fetch per call); the
        # "after" read is pinned to the receipt's own block_hash instead.
        assert client.head_block_calls == 1

    def test_a_readback_mismatch_is_reported_not_silently_trusted(self):
        # The chain reports success, but the amount actually moved (as an
        # independent read-back proves) does not match what was intended --
        # simulated here by a client that moves a DIFFERENT amount than the
        # call named, the exact shape a real call_params/field-name bug or
        # chain-side quirk would produce.
        class DesyncedClient(FakeSubstrateClient):
            def submit_extrinsic(self, signed, *, wait_for_inclusion, wait_for_finalization):
                self.submitted.append((signed, wait_for_inclusion, wait_for_finalization))
                # Moves 1 rao MORE than the call actually named.
                hotkey = signed["call"]["call_params"]["hotkey"]
                amount = signed["call"]["call_params"]["alpha_amount"]
                self.holdings[hotkey] -= amount + 1
                return FakeReceipt()

        client = DesyncedClient(holdings={HOTKEY: 100_000})
        outcome = submit_and_verify_deposit(
            a_transaction(30_000),
            signer=FakeKeypair(),
            chain_client=client,
            hotkey=HOTKEY,
            coldkey=COLDKEY,
            netuid=NETUID,
        )
        assert outcome.matches is False
        assert outcome.expected_amount_rao == 30_000
        assert outcome.observed_amount_rao == 30_001

    def test_a_chain_level_failure_still_raises_rather_than_returning_a_false_match(self):
        client = FakeSubstrateClient(
            holdings={HOTKEY: 100_000},
            receipt_factory=lambda: FakeReceipt(is_success=False, error_message="rejected"),
        )
        with pytest.raises(DepositSubmissionFailed):
            submit_and_verify_deposit(
                a_transaction(),
                signer=FakeKeypair(),
                chain_client=client,
                hotkey=HOTKEY,
                coldkey=COLDKEY,
                netuid=NETUID,
            )

    def test_a_receipt_with_no_block_hash_is_refused_not_trusted(self):
        client = FakeSubstrateClient(
            holdings={HOTKEY: 100_000},
            receipt_factory=lambda: FakeReceipt(block_hash=None),
        )
        with pytest.raises(DepositSubmissionFailed):
            submit_and_verify_deposit(
                a_transaction(),
                signer=FakeKeypair(),
                chain_client=client,
                hotkey=HOTKEY,
                coldkey=COLDKEY,
                netuid=NETUID,
            )

    def test_outcome_is_json_safe(self):
        client = FakeSubstrateClient(holdings={HOTKEY: 100_000})
        outcome = submit_and_verify_deposit(
            a_transaction(30_000),
            signer=FakeKeypair(),
            chain_client=client,
            hotkey=HOTKEY,
            coldkey=COLDKEY,
            netuid=NETUID,
        )
        import json

        json.dumps(outcome.to_evidence_dict())


class _DecodedScaleValue:
    """Minimal stand-in for a real decoded scale object, exposing only the
    one attribute (``.value``) that ``scalecodec.base.
    RuntimeConfigurationObject.update_from_scale_info_types`` and
    ``get_decoder_class_for_scale_info_definition`` actually read off a
    scale_info registry entry (confirmed by reading the installed,
    compiled library's own embedded source, ``scalecodec/base.c`` --
    ``scale_info_type['id'].value``, ``scale_info_type.value['path']``,
    ``scale_info_type.value['def']``). Not a mock of any encoding
    behaviour -- the real ``RuntimeConfigurationObject``,
    ``scalecodec.types.Tuple``, ``scalecodec.types.Struct``, and
    primitive ``u16`` decoder classes below are the real, installed
    library, doing real SCALE encode/decode work."""

    def __init__(self, value):
        self.value = value


class TestNetUidEncodingAgainstTheRealScalecodecLibrary:
    """NetUid encoding through the installed SCALE implementation.

    ``deposit_submission.py``'s ``_call_params`` supplies these as plain
    Python ``int``s. This proves that is correct -- not merely simpler --
    by actually encoding and decoding through the real, installed
    ``scalecodec`` library (the same one ``compose_call`` uses internally
    via ``create_scale_object``), never a hand-rolled reimplementation of
    SCALE encoding.

    Neither ``bittensor`` nor ``async-substrate-interface`` nor
    ``scalecodec`` hardcodes ``NetUid`` anywhere (confirmed: the only hit
    for that literal name across this worktree's installed
    ``.venv-validator`` is ``bittensor/_generated/calls.py``'s unused
    ``NetUid = Any`` type-hint alias). Its real shape is resolved entirely
    from the chain's own scale_info type registry, fetched fresh at
    connection time -- this offline session captured the real chain's own
    ``typeName`` STRING (``'NetUid'``) but not the full registry
    definition behind it (that needs a fresh
    ``get_metadata_call_function``-style read this ticket's Builder was
    told not to attempt; see the ticket's own Builder report for what
    additional live evidence would close that last gap). So every shape
    the real chain's registry could plausibly assign a small ID-wrapper
    type like this is exercised below, per
    ``get_decoder_class_for_scale_info_definition``'s own branching
    (``scalecodec/base.c``): a bare primitive alias, an unnamed-field
    (tuple-struct) composite, and a named-field (struct) composite. All
    three encode a plain ``int`` identically and correctly.
    """

    @staticmethod
    def _registry_with_u16_primitive_only(netuid_type_id):
        from scalecodec.base import RuntimeConfigurationObject

        config = RuntimeConfigurationObject(implements_scale_info=True)
        config.update_from_scale_info_types(
            [
                {
                    "id": _DecodedScaleValue(netuid_type_id),
                    "type": _DecodedScaleValue({"path": [], "def": {"primitive": "u16"}}),
                }
            ]
        )
        return config

    @staticmethod
    def _registry_with_single_field_composite(netuid_type_id, u16_type_id, *, named_field):
        from scalecodec.base import RuntimeConfigurationObject

        config = RuntimeConfigurationObject(implements_scale_info=True)
        field = {
            "name": "value" if named_field else None,
            "type": u16_type_id,
            "typeName": "u16",
            "docs": [],
        }
        config.update_from_scale_info_types(
            [
                {
                    "id": _DecodedScaleValue(u16_type_id),
                    "type": _DecodedScaleValue({"path": [], "def": {"primitive": "u16"}}),
                },
                {
                    "id": _DecodedScaleValue(netuid_type_id),
                    "type": _DecodedScaleValue(
                        {
                            "path": ["pallet_subtensor", "types", "NetUid"],
                            "def": {"composite": {"fields": [field]}},
                        }
                    ),
                },
            ]
        )
        return config

    def _assert_int_round_trips(self, config, type_string, netuid_value):
        from scalecodec.base import ScaleBytes

        encoded = config.create_scale_object(type_string).encode(netuid_value)
        # A u16 netuid encodes as exactly 2 little-endian bytes -- confirms
        # this actually reached the primitive encoder, not merely returned
        # something.
        assert encoded.data == netuid_value.to_bytes(2, "little")

        decoded_object = config.create_scale_object(type_string, data=ScaleBytes(encoded.data))
        decoded_value = decoded_object.decode()
        # The Struct (named-field composite) shape decodes to {'value': N}
        # rather than a bare int -- both are confirmed real library
        # behaviour (see this class's own docstring), so either is an
        # accepted round trip of the same underlying integer.
        if isinstance(decoded_value, dict):
            assert len(decoded_value) == 1
            (decoded_value,) = decoded_value.values()
        assert decoded_value == netuid_value

    def test_plain_int_round_trips_when_netuid_is_a_bare_primitive_alias(self):
        config = self._registry_with_u16_primitive_only(netuid_type_id=41)
        self._assert_int_round_trips(config, "scale_info::41", NETUID)

    def test_plain_int_round_trips_when_netuid_is_an_unnamed_single_field_tuple_struct(self):
        config = self._registry_with_single_field_composite(
            netuid_type_id=41, u16_type_id=7, named_field=False
        )
        self._assert_int_round_trips(config, "scale_info::41", NETUID)

    def test_plain_int_round_trips_when_netuid_is_a_named_single_field_struct(self):
        config = self._registry_with_single_field_composite(
            netuid_type_id=41, u16_type_id=7, named_field=True
        )
        self._assert_int_round_trips(config, "scale_info::41", NETUID)
