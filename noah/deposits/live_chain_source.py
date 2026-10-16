"""Chain reads used by deposit planning and receipt verification.

Use the finalized head and explicitly pinned historical position reads. Missing
storage and failed queries are different outcomes. Endpoint selection requires a
WebSocket URL; caller-supplied connection factories may provide compatible clients."""

import os
from urllib.parse import urlsplit

from ..chain.sources import Refusal, connect_scrubbed, endpoint_label, scrub_endpoint_text


class LiveChainSourceUnavailable(Refusal):
    """A read against the real connection failed, or answered with
    something that is not the shape the caller asked for. Nothing is KNOWN
    rather than nothing FOUND -- the identical discipline every other
    ``_ask``-style reader in this codebase already applies."""


class LiveChainSourceMisconfigured(Refusal):
    """The environment/configuration needed to build a real connection is
    missing or malformed -- refused before any connection is attempted."""


STAKE_API = "StakeInfoRuntimeApi"

ENDPOINT_ENV_VARS = ("NOAH_AUTHOR_WS", "NOAH_READER_WS", "NOAH_AUTHOR_RPC", "NOAH_READER_RPC")


class LiveChainSource:
    """Read finalized head, historical holdings and the chain inputs needed for a deposit."""

    def __init__(self, connection, *, endpoint):
        self._connection = connection
        self.endpoint = endpoint

    def head_block(self):
        """Return the current finalized head block number."""
        head_hash = self._ask(
            "the finalized chain head hash",
            lambda: self._connection.get_chain_finalised_head(),
        )
        return self._ask(
            f"the block number of head {head_hash}",
            lambda: self._connection.get_block_number(head_hash),
        )

    def block_hash(self, block):
        return self._ask(
            f"the hash of block {block}", lambda: self._connection.get_block_hash(block)
        )

    def block_timestamp(self, block):
        """``block``'s own on-chain time, in whole UTC seconds -- the exact
        read (``Timestamp.Now``, milliseconds, block-pinned via its own
        hash) adopted ``LiveRunnerChain.block_timestamp`` already uses,
        confirmed there against the pinned runtime."""
        block_hash = self.block_hash(block)
        return self._ask(
            f"the time of block {block}",
            lambda: self._unwrap(
                self._connection.query("Timestamp", "Now", [], block_hash=block_hash)
            )
            // 1000,
        )

    def holding_at(self, *, hotkey, coldkey, netuid, block=None, block_hash=None):
        """This position's live stake, in rao, pinned to exactly one block
        -- named by block NUMBER (the hourly sampler's own currency, from
        its own binary search over block numbers) or by block HASH (a
        submission receipt's own currency), never both and never neither:
        a caller comparing two reads across time must pin each one
        explicitly, rather than this method silently reading the
        connection's own current head twice and comparing two different
        moments as though they were the same one.
        """
        if (block is None) == (block_hash is None):
            raise LiveChainSourceUnavailable(
                "holding_at requires exactly one of block or block_hash, not both and not "
                "neither -- an unpinned read would silently use the connection's own current "
                "head, which could differ between two calls a caller is comparing."
            )
        pin = block_hash if block_hash is not None else self.block_hash(block)
        return self._ask(
            f"stake info for {hotkey}/{coldkey}/{netuid} at {pin}",
            lambda: self._stake_amount(
                self._connection.runtime_call(
                    STAKE_API,
                    "get_stake_info_for_hotkey_coldkey_netuid",
                    [hotkey, coldkey, netuid],
                    block_hash=pin,
                )
            ),
        )

    def storage_map_key_count(self, name):
        entry = self._ask(
            f"the metadata for SubtensorModule.{name}",
            lambda: self._connection.get_metadata_storage_function("SubtensorModule", name),
        )
        hashers = (
            (entry.get("type") or {}).get("Map", {}).get("hashers")
            if isinstance(entry, dict)
            else None
        )
        if not isinstance(hashers, (list, tuple)):
            raise LiveChainSourceUnavailable(
                f"SubtensorModule.{name} has no readable map key definition."
            )
        return len(hashers)

    def stake_info(self, *, hotkey, coldkey, netuid):
        return self._stake_call(
            "get_stake_info_for_hotkey_coldkey_netuid", [hotkey, coldkey, netuid]
        )

    def stake_availability(self, *, coldkey, netuid):
        answer = self._stake_call("get_stake_availability_for_coldkeys", [[coldkey], [netuid]])
        if not isinstance(answer, dict):
            raise LiveChainSourceUnavailable("Stake availability did not return a coldkey map.")
        per_coldkey = answer.get(coldkey)
        if not isinstance(per_coldkey, dict):
            raise LiveChainSourceUnavailable("Stake availability omitted the requested coldkey.")
        return per_coldkey.get(netuid, per_coldkey.get(str(netuid)))

    def miner_collateral(self, *, netuid, hotkey, coldkey):
        return self._subtensor_query("MinerCollateral", [netuid, hotkey, coldkey])

    def coldkey_collateral(self, *, netuid, coldkey):
        value = self._subtensor_query("ColdkeyMinerCollateral", [netuid, coldkey])
        return 0 if value is None else value

    def collateral_hotkeys(self, *, netuid, coldkey):
        value = self._subtensor_query("ColdkeyCollateralHotkeys", [netuid, coldkey])
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise LiveChainSourceUnavailable("Collateral hotkey index did not return a list.")
        return list(value)

    def subtoken_enabled(self, netuid):
        return self._subtensor_query("SubtokenEnabled", [netuid])

    def transfer_toggle(self, netuid):
        return self._subtensor_query("TransferToggle", [netuid])

    def _subtensor_query(self, name, keys):
        return self._ask(
            f"SubtensorModule.{name}{keys}",
            lambda: self._connection.query("SubtensorModule", name, keys),
        )

    def _stake_call(self, method, params):
        return self._ask(
            f"{STAKE_API}.{method}{params}",
            lambda: self._connection.runtime_call(STAKE_API, method, params),
        )

    @staticmethod
    def _stake_amount(raw):
        value = raw.value if hasattr(raw, "value") else raw
        if isinstance(value, dict) and "stake" in value:
            stake = value["stake"]
            if isinstance(stake, bool) or not isinstance(stake, int) or stake < 0:
                raise LiveChainSourceUnavailable(
                    f"stake info's own 'stake' field decoded as {stake!r}, not a real, "
                    f"non-negative decoded integer. Nothing is trusted from it."
                )
            return stake
        raise LiveChainSourceUnavailable(
            f"stake info decoded as {value!r}, with no readable 'stake' field. Nothing is "
            f"trusted from it."
        )

    @staticmethod
    def _unwrap(value):
        return value.value if hasattr(value, "value") else value

    def _ask(self, what, question):
        """Convert failed chain queries to endpoint-redacted refusals; preserve existing refusals."""
        try:
            answer = question()
        except Refusal:
            raise
        except Exception as failure:
            reason = scrub_endpoint_text(failure, self.endpoint)
            raise LiveChainSourceUnavailable(
                f"asking {endpoint_label(self.endpoint)} for {what} failed: {reason}. "
                f"Nothing is KNOWN rather than nothing FOUND."
            ) from None
        return self._unwrap(answer)


def live_chain_source_from_environment(env_var, *, connection_factory=None):
    """Build a deposit chain reader from a configured WebSocket endpoint.

    Refuse absent or unsupported endpoint schemes. The supplied factory determines
    the concrete client, while historical reads retain their explicit block identity."""
    if env_var not in ENDPOINT_ENV_VARS:
        raise LiveChainSourceMisconfigured(
            f"env_var must be one of {ENDPOINT_ENV_VARS}, got a name that is not shown."
        )
    url = os.environ.get(env_var)
    if not url:
        raise LiveChainSourceMisconfigured(
            f"{env_var} is not set. A live chain source cannot be built without an endpoint "
            f"URL, and there is no default: this ticket assumes nothing about which "
            f"private-server endpoint is meant."
        )
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("ws", "wss"):
        raise LiveChainSourceMisconfigured(
            f"{env_var} has the scheme {scheme or '(none)'!r}, not ws:// or "
            f"wss://. The connection factory this function builds by default "
            f"(async_substrate_interface.SubstrateInterface) requires a ws/wss endpoint -- its "
            f"underlying websockets client refuses an http(s):// URL immediately, before any "
            f"socket opens (the exact crash issue #15's own live QA hit: "
            f"websockets.exceptions.InvalidURI: ... scheme isn't ws or wss). Refusing here, by "
            f"name, rather than letting that library raise its own confusing error later. If "
            f"{env_var} is meant to be an RPC-shaped endpoint, use the matching WS environment "
            f"variable instead -- this function always needs a ws/wss URL, an RPC-only "
            f"environment is not a valid configuration for it."
        )

    if connection_factory is None:

        def connection_factory():
            from async_substrate_interface import SubstrateInterface

            return SubstrateInterface(url=url, auto_discover=False)

    connection = connect_scrubbed(connection_factory, url, LiveChainSourceUnavailable)
    return LiveChainSource(connection, endpoint=url)
