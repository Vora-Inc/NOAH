"""A small fake chain that turns composed calls into the events and extrinsics a
real chain would produce, so tests exercise compose -> chain -> reader end to end.

The event and extrinsic shapes are copied from the reader's own recorded-shape
tests (noah/chain/tests/test_deposits_stake_and_hotkey_transferred.py and
scripts/tests/test_batch_miner_provisioning.py). Nothing here is a real chain.
"""

import hashlib

from noah.chain.ss58 import address_of
from noah.deposits.assistant.config import AssistantConfig

MINER = address_of(bytes([1]) * 32)
COLD = address_of(bytes([2]) * 32)
ARK = address_of(bytes([3]) * 32)
VAL = address_of(bytes([4]) * 32)
OTHER_VAL = address_of(bytes([5]) * 32)
NETUID = 2
N = 1_000_000_000


def config(**overrides):
    base = dict(
        network="private-test",
        endpoint="ws://x",
        netuid=NETUID,
        miner_hotkey=MINER,
        coldkey=COLD,
        wallet_name="w",
        hotkey_name="h",
        ark_coldkey=ARK,
        validator_hotkey=VAL,
        mode="maintenance",
        percent_bp=5000,
        amount_rao=None,
        cap_rao=None,
        minimum_rao=0,
        reserve_rao=1 * N,
        evidence_file="e.json",
        state_dir=".",
        stake_position="miner",
        genesis_hash=None,
        wallet_path=None,
    )
    base.update(overrides)
    return AssistantConfig(**base)


class FakeSource:
    def __init__(
        self,
        stake=1000 * N,
        stake_info_record="default",
        available=None,
        subtoken=True,
        transfer_toggle=True,
    ):
        # The EFFECTIVE values the chain's runtime reports (stored value, or its own default when
        # nothing is stored). Model "absent" by giving the default the runtime would apply:
        # SubtokenEnabled absent -> False, TransferToggle absent -> True (measured on spec 467).
        self.subtoken = subtoken
        self.transfer_toggle_value = transfer_toggle
        self.stake = stake
        self.stake_info_record = stake_info_record  # replaces the whole answer when not "default"
        self.available = stake if available is None else available
        self.stake_info_hotkeys = []

    def subtoken_enabled(self, netuid):
        return self.subtoken

    def transfer_toggle(self, netuid):
        return self.transfer_toggle_value

    def storage_map_key_count(self, name):
        return {"MinerCollateral": 3, "ColdkeyMinerCollateral": 2}.get(name, 2)

    def collateral_hotkeys(self, *, netuid, coldkey):
        return []

    def coldkey_collateral(self, *, netuid, coldkey):
        return 0

    def miner_collateral(self, *, netuid, hotkey, coldkey):
        return None

    def stake_info(self, *, hotkey, coldkey, netuid):
        self.stake_info_hotkeys.append(hotkey)
        if self.stake_info_record != "default":
            return self.stake_info_record
        return {"stake": self.stake, "locked": 0, "is_registered": True}

    def stake_availability(self, *, coldkey, netuid):
        return {"total": self.stake, "locked": 0, "available": self.available}


class FakeKeypair:
    def __init__(self, address=COLD, reject=False):
        self.ss58_address = address
        self.reject = reject
        self.signed = 0

    def sign(self, data):
        if self.reject:
            raise RuntimeError("user rejected on device")
        if not isinstance(data, bytes):
            raise TypeError("sign needs bytes")
        self.signed += 1
        return b"\0" * 64


class Receipt:
    def __init__(self, ok, block_hash, ext_hash, error=None):
        self.is_success, self.block_hash, self.extrinsic_hash, self.error_message = (
            ok,
            block_hash,
            ext_hash,
            error,
        )


def _arg(call, name):
    return call["call_params"][name]


class FakeChain:
    """Block 0 is genesis. ``finality_lag`` blocks separate head from the finalized head."""

    def __init__(self, finality_lag=0, owner=COLD, owners=None):
        self.blocks = [{"events": [], "extrinsics": []}]
        self.finality_lag = finality_lag
        self.owners = owners if owners is not None else {MINER: owner}
        self.submit_mode = (
            "ok"  # ok | raise_after_inclusion | raise_before_inclusion | extrinsic_fails
        )
        self.submitted = []
        self.sign_eras = []
        self.wrong_validator = None  # make the chain land stake on this validator instead

    # ---- blocks
    def advance(self, n=1):
        for _ in range(n):
            self.blocks.append({"events": [], "extrinsics": []})

    @property
    def head(self):
        return len(self.blocks) - 1

    def get_block_hash(self, n):
        if n > self.head:
            raise KeyError(n)
        return f"0xblock{n:04d}"

    def get_block_number(self, block_hash):
        return int(str(block_hash)[7:])

    def get_chain_head(self):
        return self.get_block_hash(self.head)

    def get_chain_finalised_head(self):
        return self.get_block_hash(max(0, self.head - self.finality_lag))

    def get_events(self, *, block_hash):
        return list(self.blocks[self.get_block_number(block_hash)]["events"])

    def get_block(self, block_hash):
        return {"extrinsics": list(self.blocks[self.get_block_number(block_hash)]["extrinsics"])}

    def query(self, module, storage_function, params, block_hash=None):
        assert (module, storage_function) == ("SubtensorModule", "Owner")
        return self.owners.get(params[0])

    # ---- signing and submitting
    def compose_call(self, *, call_module, call_function, call_params):
        return {
            "call_module": call_module,
            "call_function": call_function,
            "call_params": call_params,
        }

    def create_signed_extrinsic(self, *, call, keypair, era=None):
        self.sign_eras.append(era)
        keypair.sign(bytearray(b"payload"))
        return {"call": call, "signer": keypair.ss58_address}

    def submit_extrinsic(self, signed, wait_for_inclusion=False, wait_for_finalization=False):
        self.submitted.append(signed)
        if self.submit_mode == "raise_before_inclusion":
            raise ConnectionError("socket closed")
        events, extrinsic = self._effects(signed)
        if self.submit_mode == "extrinsic_fails":
            self.advance()
            return Receipt(False, self.get_block_hash(self.head), "0xext", "StakeUnavailable")
        self.advance()
        block = self.blocks[self.head]
        block["events"], block["extrinsics"] = events, [extrinsic]
        if self.submit_mode == "raise_after_inclusion":
            raise ConnectionError("lost the connection after sending")
        return Receipt(True, self.get_block_hash(self.head), "0xext")

    def _effects(self, signed):
        signer, call = signed["signer"], signed["call"]
        validator = self.wrong_validator
        events, ordinal = [], [0]

        def add(module, event, attrs):
            events.append(
                {
                    "ordinal_in_block": ordinal[0],
                    "extrinsic_idx": 0,
                    "module_id": module,
                    "event_id": event,
                    "attributes": attrs,
                }
            )
            ordinal[0] += 1

        def args(c):
            return [{"name": k, "value": v} for k, v in c["call_params"].items()]

        def leg(c):
            return {
                "call_module": c["call_module"],
                "call_function": c["call_function"],
                "call_args": args(c),
            }

        def transfer_stake(c):
            p = c["call_params"]
            add(
                "SubtensorModule",
                "StakeAdded",
                [
                    p["destination_coldkey"],
                    validator or p["hotkey"],
                    1,
                    p["alpha_amount"],
                    p["destination_netuid"],
                    0,
                ],
            )
            add(
                "SubtensorModule",
                "StakeTransferred",
                [
                    signer,
                    p["destination_coldkey"],
                    p["hotkey"],
                    p["origin_netuid"],
                    p["destination_netuid"],
                    p["alpha_amount"],
                ],
            )

        def remark(c):
            raw = c["call_params"]["remark"]
            add(
                "System",
                "Remarked",
                {"sender": signer, "hash": "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()},
            )

        name = call["call_function"]
        if name == "transfer_stake_and_hotkey":
            p = call["call_params"]
            add(
                "SubtensorModule",
                "StakeAdded",
                [
                    p["destination_coldkey"],
                    validator or p["destination_hotkey"],
                    1,
                    p["alpha_amount"],
                    p["destination_netuid"],
                    0,
                ],
            )
            add(
                "SubtensorModule",
                "StakeAndHotkeyTransferred",
                {
                    "origin_coldkey": signer,
                    "destination_coldkey": p["destination_coldkey"],
                    "origin_hotkey": p["origin_hotkey"],
                    "destination_hotkey": p["destination_hotkey"],
                    "origin_netuid": p["origin_netuid"],
                    "destination_netuid": p["destination_netuid"],
                    "amount": 1,
                },
            )
            return events, {"address": signer, "call": leg(call)}
        assert name == "batch_all"
        legs = call["call_params"]["calls"]
        for c in legs:
            {"transfer_stake": transfer_stake, "remark_with_event": remark}[c["call_function"]](c)
        raw_legs = []
        for c in legs:
            d = leg(c)
            if c["call_function"] == "remark_with_event":
                d["call_args"] = [
                    {"name": "remark", "value": "0x" + c["call_params"]["remark"].hex()}
                ]
            raw_legs.append(d)
        return events, {
            "address": signer,
            "call": {
                "call_module": "Utility",
                "call_function": "batch_all",
                "call_args": [{"name": "calls", "value": raw_legs}],
            },
        }
