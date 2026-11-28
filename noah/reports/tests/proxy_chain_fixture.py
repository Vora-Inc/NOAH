"""Synthetic RPC chain for real SCALE proxy signing and independent reader replay.

This is not a Subtensor execution engine or a live-network acceptance test.
"""

from scalecodec.base import ScaleBytes
from substrateinterface import Keypair

from noah.reports.backing.proxy_receipts import proxy_report

from .test_weekly_local_chain import Chain, _commitment_data, commitment_info


class ProxyChain(Chain):
    def __init__(self, miner, owner, extras=()):
        super().__init__(miner, owner, extras)
        self.delegate = Keypair.create_from_uri("//weekly-proxy", crypto_type=1, ss58_format=42)
        self.proxy_events = {}
        for hot, _ in [(miner, owner), *extras]:
            address = Keypair(public_key=hot, crypto_type=1, ss58_format=42).ss58_address
            self._quota_storage(
                "Proxy",
                "Proxies",
                [address],
                (
                    (
                        [
                            {
                                "delegate": self.delegate.ss58_address,
                                "proxy_type": "Validate",
                                "delay": 0,
                            }
                        ],
                    ),
                    0,
                ),
            )
        self._quota_storage(
            "System",
            "Account",
            [self.delegate.ss58_address],
            {
                "nonce": 0,
                "consumers": 0,
                "providers": 1,
                "sufficients": 0,
                "data": {"free": 10**15, "reserved": 0, "frozen": 0, "flags": 0},
            },
        )

    def call(self, method, params):
        if method == "payment_queryInfo":
            return {
                "weight": {"ref_time": 1, "proof_size": 0},
                "class": "Normal",
                "partialFee": 1000,
            }
        return super().call(method, params)

    def include(self, extrinsic):
        head = super().include(extrinsic)
        obj = self.sdk.runtime_config.create_scale_object(
            "Extrinsic", metadata=self.sdk.metadata, data=ScaleBytes(extrinsic)
        )
        obj.decode()
        routed = proxy_report(obj.value)
        assert routed, obj.value["call"]
        inner, real, delegate = routed
        assert delegate == self.delegate.public_key.hex()
        index = len(self.blocks[head]["block"]["extrinsics"]) - 1
        stored = _commitment_data(inner)
        key = self.sdk.create_storage_key("Commitments", "CommitmentOf", [82, "0x" + real])
        registration = self.sdk.runtime_config.create_scale_object(
            key.value_scale_type, metadata=self.sdk.metadata
        )
        self.commitment_scale = (
            "0x"
            + bytes(
                registration.encode(
                    {"deposit": 0, "block": 6, "info": commitment_info(*stored)}
                ).data
            ).hex()
        )
        self.quota_used += max(100, len(stored[1]))
        self._quota_storage(
            "Commitments",
            "UsedSpaceOf",
            [82, "0x" + real],
            {"last_epoch": 42, "used_space": self.quota_used},
        )
        events = self.proxy_events.setdefault(head, [])
        for module, name, attrs in (
            ("Commitments", "Commitment", {"netuid": 82, "who": "0x" + real}),
            ("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
            (
                "System",
                "ExtrinsicSuccess",
                {
                    "dispatch_info": {
                        "weight": {"ref_time": 0, "proof_size": 0},
                        "class": "Normal",
                        "pays_fee": "Yes",
                    }
                },
            ),
        ):
            events.append(
                {"phase": {"ApplyExtrinsic": index}, "event": {module: {name: attrs}}, "topics": []}
            )
        event_type = self.sdk.create_storage_key("System", "Events", []).value_scale_type
        encoded = self.sdk.runtime_config.create_scale_object(
            event_type, metadata=self.sdk.metadata
        )
        self.events[self.event_key][head] = "0x" + bytes(encoded.encode(events).data).hex()
        return head
