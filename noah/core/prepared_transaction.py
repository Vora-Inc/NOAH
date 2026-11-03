"""Pinned Bittensor 11.1 direct transport: prepare, persist, send exact bytes once.

The SDK's process-local nonce cache is deliberately bypassed. There is no retry
or signing operation in recovery. Private SDK bridging is isolated here because
its public submit_signature combines byte assembly and transmission.
"""

from dataclasses import dataclass
from hashlib import blake2b, sha256

from .reader import ChainUnreadable

ERA_PERIOD = 128


def transaction_hash(data):
    return "0x" + blake2b(data, digest_size=32).hexdigest()


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ChainUnreadable(f"invalid prepared transaction {name}")
    return value


def _raw(client):
    # Exact pinned SDK contract. An incompatible client fails before signing/send.
    return client._client._substrate.raw


def rpc(client, method, params):
    return client._call(_raw(client).rpc_request(method, params))


def signed_bytes_at(client, block_hash, index):
    block = rpc(client, "chain_getBlock", [block_hash])
    try:
        return bytes.fromhex(block["block"]["extrinsics"][index].removeprefix("0x"))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        raise ChainUnreadable("exact included transaction bytes unavailable") from error


@dataclass
class Prepared:
    signed: object
    evidence: dict


class SDKPreparedTransport:
    def __init__(self, client, wallet):
        self.client, self.wallet = client, wallet

    def _live_runtime(self):
        runtime = rpc(self.client, "state_getRuntimeVersion", [None])
        return {
            key: _integer(runtime.get(key), key)
            for key in ("specVersion", "transactionVersion")
        }

    def prepare(self, call, chain):
        client, wallet = self.client, self.wallet
        client._client._executor._enforce_raw_call(None)
        head = _integer(chain.head_block(), "head")
        agreed = chain.signing_context(wallet.ss58_address, head)
        # prepare_call -> prepare_unsigned fetches account_nextIndex uncached.
        unsigned = client.prepare_call(
            call,
            address=wallet.ss58_address,
            crypto_type=int(wallet.crypto_type),
            period=ERA_PERIOD,
        )
        runtime = self._live_runtime()  # uncached RPC, not SDK head-codec TTL
        if (
            unsigned.address != wallet.ss58_address
            or unsigned.genesis_hash != chain.genesis_hash()
            or unsigned.nonce != agreed["nonce"]
            or runtime != {key: agreed[key] for key in runtime}
            or unsigned.spec_version != runtime["specVersion"]
            or unsigned.transaction_version != runtime["transactionVersion"]
        ):
            raise ChainUnreadable(
                "prepared signer/nonce/genesis/runtime differs from finalized readers"
            )
        _integer(unsigned.nonce, "nonce")
        era = unsigned.era
        if (
            not isinstance(era, dict)
            or era.get("period") != ERA_PERIOD
            or type(era.get("current")) is not int
        ):
            raise ChainUnreadable(
                "prepared transaction has no exact bounded mortal era"
            )
        current = era["current"]
        # At period128 the SCALE mortality phase is unquantized: birth=current.
        # Require this independently finalized point so no best-head assumption
        # becomes the recorded mortality proof.
        if current > chain.head_block() or current < head:
            raise ChainUnreadable(
                "prepared mortality head is not current finalized history"
            )
        birth = current
        if unsigned.era_block_hash != chain.block_hash(birth):
            raise ChainUnreadable(
                "prepared mortality birth hash differs from finalized readers"
            )
        composed = client.compose(call)
        if bytes(composed.data) != bytes(unsigned.call_data):
            raise ChainUnreadable("prepared call bytes changed")
        from bittensor._transport.extrinsics import sign_unsigned

        signature = client._call(sign_unsigned(unsigned, wallet))
        signed = client._call(_raw(client).attach_signature(unsigned, signature))
        data = bytes(signed.data)
        digest = transaction_hash(data)
        if signed.extrinsic_hash != digest:
            raise ChainUnreadable(
                "SDK signed transaction hash disagrees with exact bytes"
            )
        evidence = {
            "schema": 1,
            "signed_hex": "0x" + data.hex(),
            "extrinsic_hash": digest,
            "call_sha256": sha256(bytes(unsigned.call_data)).hexdigest(),
            "nonce": unsigned.nonce,
            "signer": wallet.ss58_address,
            "genesis_hash": unsigned.genesis_hash,
            "head_at_send": head,
            "spec_version": unsigned.spec_version,
            "transaction_version": unsigned.transaction_version,
            "era": era,
            "birth": birth,
            "death": birth + ERA_PERIOD,
            "birth_hash": unsigned.era_block_hash,
        }
        return Prepared(signed, evidence)

    def send(self, prepared, chain):
        e = prepared.evidence
        if transaction_hash(bytes(prepared.signed.data)) != e["extrinsic_hash"]:
            raise ChainUnreadable("prepared transaction bytes changed before send")
        if self._live_runtime() != {
            "specVersion": e["spec_version"],
            "transactionVersion": e["transaction_version"],
        }:
            raise ChainUnreadable("runtime changed after preparation; no send")
        head = chain.head_block()
        if (
            not e["birth"] <= head < e["death"]
            or chain.block_hash(e["birth"]) != e["birth_hash"]
        ):
            raise ChainUnreadable(
                "prepared transaction mortality is no longer valid; no send"
            )
        nonce = _integer(rpc(self.client, "account_nextIndex", [e["signer"]]), "nonce")
        agreed = chain.signing_context(e["signer"], head)
        if (
            agreed["specVersion"] != e["spec_version"]
            or agreed["transactionVersion"] != e["transaction_version"]
        ):
            raise ChainUnreadable(
                "finalized runtime changed after preparation; no send"
            )
        if nonce != e["nonce"] or agreed["nonce"] != e["nonce"]:
            raise ChainUnreadable("signer nonce changed after preparation; no send")
        # No re-signing, cache update or automatic retry here.
        return self.client._call(
            self.client._client._substrate.submit_signed(
                prepared.signed,
                self.wallet,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
        )
