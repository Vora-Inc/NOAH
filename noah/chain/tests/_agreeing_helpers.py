"""Shared synthetic inputs for public participant tests."""

from noah.chain.agreeing import AgreeingChain


class Fake:
    """One endpoint. Answers what it is told to answer."""

    def __init__(
        self,
        head=1000,
        hashes=None,
        times=None,
        events=None,
        canary=256,
        fail=None,
        alpha=None,
        first_emission_block=None,
        published_ark_pin=None,
        extrinsics=None,
        owners_at_hash=None,
        maps_declared=None,
    ):
        self._head = head
        self._maps_declared = maps_declared or {"Alpha": True, "TotalHotkeyShares": True}
        self._hashes = hashes or {}
        self._times = times or {}
        self._events = events or {}
        self._canary = canary
        self._fail = fail
        self._alpha = alpha or {}
        self._extrinsics = extrinsics or {}
        self._owners_at_hash = owners_at_hash or {}
        self._first_emission_block = first_emission_block
        self._published_ark_pin = published_ark_pin
        self.asked = []
        self.closed = False

    def first_emission_block(self, block):
        """Return the configured start block, or None when no start has been recorded."""
        self.asked.append(("first_emission_block", block))
        self._maybe_fail("first_emission_block")
        return self._first_emission_block

    def published_ark_pin(self, block):
        """Return the configured Ark identity, or None when no identity has been recorded."""
        self.asked.append(("published_ark_pin", block))
        self._maybe_fail("published_ark_pin")
        return self._published_ark_pin

    def holding_maps_declared(self, block):
        self.asked.append(("holding_maps_declared", block))
        self._maybe_fail("holding_maps_declared")
        return dict(self._maps_declared)

    def alpha_share(self, hotkey, coldkey, block):
        """Return the configured wallet share, preserving zero separately from absence."""
        self.asked.append(("alpha_share", block))
        return self._alpha.get((hotkey, coldkey, block))

    def _maybe_fail(self, what):
        if self._fail == what:
            raise RuntimeError("the node hung up")

    def head_block(self):
        self._maybe_fail("head")
        return self._head

    def block_hash(self, block):
        self.asked.append(("hash", block))
        self._maybe_fail("hash")
        return self._hashes.get(block, f"0x{block:x}")

    def block_timestamp(self, block):
        self.asked.append(("time", block))
        self._maybe_fail("time")
        return self._times.get(block, 1787000000 + block)

    def events(self, block):
        self.asked.append(("events", block))
        self._maybe_fail("events")
        return self._events.get(block, [])

    def canary(self, block):
        self._maybe_fail("canary")
        return self._canary

    def extrinsics(self, block):
        self.asked.append(("extrinsics", block))
        self._maybe_fail("extrinsics")
        return self._extrinsics.get(block, _NoBatchAllExtrinsics())

    def owner_at(self, *, hotkey, block_hash):
        self.asked.append(("owner_at", hotkey, block_hash))
        self._maybe_fail("owner_at")
        return self._owners_at_hash[hotkey, block_hash]

    def close(self):
        self.closed = True


class _NoBatchAllExtrinsics(list):
    """Return a non-batch extrinsic shape for events without configured extrinsic data."""

    def __len__(self):
        return 2**31

    def __getitem__(self, index):
        if isinstance(index, int) and (not isinstance(index, bool)) and (0 <= index < len(self)):
            return {}
        raise IndexError(index)


NETUID = 35
ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
VALIDATOR_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
SEAT0_HOT = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
SEAT0_COLD = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
SEAT1_HOT = "5CG7y1S6v9TmRDm32VAeUbyMpfSyRvrNmFLcF8kekVt3zaQs"
SEAT1_COLD = "5HNCnaAFBCUiUn4mkZkfXURwvSScyrBYVhMN9t7dvkHZGCrR"
OWNER_COLD = "5EqZVDbW5sKXE5qB5zVaH6SQT2sud3LkmFBo2dn79NdY9RTr"
OWNER_HOT = "5EsmkLf4VnpgNM31syMjAWsUrQdW2Yu5xzWbv6oDQydP9vVx"
EPOCH = 900
ANCHOR = 1787000000


def pay_event(amounts, netuid=NETUID, mechanism=0):
    return {
        "ordinal_in_block": 3,
        "extrinsic_idx": 1,
        "module_id": "SubtensorModule",
        "event_id": "IncentiveAlphaEmittedToMiners",
        "attributes": {"netuid": netuid + mechanism * 4096, "emissions": amounts},
    }


class FullSource(Fake):
    """Answer runner reads from configurable synthetic seats, owners, holdings and metadata."""

    def __init__(
        self,
        seats=None,
        registrations=None,
        n=None,
        mechanisms=1,
        owners=None,
        subnet_owner_pair=(OWNER_COLD, OWNER_HOT),
        first_emission_block=None,
        published_ark_pin=ARK,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._seats = seats if seats is not None else {0: SEAT0_HOT, 1: SEAT1_HOT}
        self._registrations = (
            registrations if registrations is not None else {seat: 100 for seat in self._seats}
        )
        self._n = n if n is not None else len(self._seats)
        self._mechanisms = mechanisms
        self._owners = owners or {
            SEAT0_HOT: SEAT0_COLD,
            SEAT1_HOT: SEAT1_COLD,
            OWNER_HOT: OWNER_COLD,
        }
        self._subnet_owner_pair = subnet_owner_pair
        self._first_emission_block = first_emission_block
        self._published_ark_pin = published_ark_pin

    def seat_hotkeys(self, block):
        self.asked.append(("seats", block))
        return dict(self._seats)

    def registration_blocks(self, block):
        self.asked.append(("registrations", block))
        return dict(self._registrations)

    def subnetwork_n(self, block):
        self.asked.append(("n", block))
        return self._n

    def mechanism_count(self, block):
        self.asked.append(("mechanisms", block))
        return self._mechanisms

    def hotkey_owners(self, hotkeys, block):
        self.asked.append(("owners", block, tuple(sorted(hotkeys))))
        return {h: self._owners[h] for h in hotkeys if h in self._owners}

    def subnet_owner(self, block):
        self.asked.append(("subnet_owner", block))
        return self._subnet_owner_pair

    def staking_hotkeys(self, coldkey, block):
        return []

    def alpha_share(self, hotkey, coldkey, block):
        from noah.chain.holdings import Share

        return Share(encoding="safefloat", raw=0, exponent=0, source="absent")

    def hotkey_shares(self, hotkey, block):
        return None

    def hotkey_alpha(self, hotkey, block):
        return 0

    def coldkey_collateral(self, coldkey, block):
        """Return zero collateral for this synthetic fixture."""
        return 0

    def collateral_hotkeys(self, coldkey, block):
        return ()

    def miner_collateral(self, hotkey, coldkey, block):
        return None

    def first_emission_block(self, block):
        self.asked.append(("first_emission_block", block))
        return self._first_emission_block

    def published_ark_pin(self, block):
        self.asked.append(("published_ark_pin", block))
        return self._published_ark_pin

    def collateral_arity(self, block):
        """Return the declared key count for each collateral storage map."""
        return {"ColdkeyMinerCollateral": 2, "MinerCollateral": 3, "ColdkeyCollateralHotkeys": 2}


def a_gated_chain(**kwargs):
    return AgreeingChain(
        sources={"archive": FullSource(**kwargs), "latent": FullSource(**kwargs)},
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    )


__all__ = [
    "ANCHOR",
    "ARK",
    "EPOCH",
    "FullSource",
    "NETUID",
    "SEAT0_COLD",
    "SEAT0_HOT",
    "SEAT1_COLD",
    "SEAT1_HOT",
    "VALIDATOR_HOTKEY",
    "a_gated_chain",
    "pay_event",
]
