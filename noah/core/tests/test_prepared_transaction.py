"""No RPC/real keys: exercise exact prepared-byte boundaries and durable recovery."""

import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

from ..prepared_transaction import SDKPreparedTransport, transaction_hash
from ..publish_intent import PublishIntent
from ..reader import ChainUnreadable


class Chain:
    nonce = 215
    head = 100
    runtime = {"specVersion": 486, "transactionVersion": 1}

    def head_block(self):
        return self.head

    def block_hash(self, block):
        return f"0x{block:064x}"

    def genesis_hash(self):
        return self.block_hash(0)

    def signing_context(self, address, block):
        return {"nonce": self.nonce, **self.runtime}


class Wallet:
    ss58_address = "offline-signer"
    crypto_type = 1
    signatures = 0

    def sign(self, payload):
        self.signatures += 1
        return b"x" * 64


class Client:
    def __init__(self, chain):
        self.chain = chain
        self.cached_nonce = 214  # the dangerous cache must never be consulted
        self._client = SimpleNamespace(
            _substrate=self, _executor=SimpleNamespace(_enforce_raw_call=lambda _: None)
        )
        self.raw = self
        self.sent = []
        self.prepared = []
        self.lose_reply = False

    def _call(self, awaitable):
        return asyncio.run(awaitable)

    def compose(self, call):
        return SimpleNamespace(data=call)

    def prepare_call(self, call, *, address, crypto_type, period):
        # The installed public API delegates to prepare_unsigned, whose nonce
        # lookup uses use_cache=False. Record that fresh result explicitly.
        self.prepared.append(self.chain.nonce)
        return SimpleNamespace(
            address=address,
            genesis_hash=self.chain.genesis_hash(),
            nonce=self.chain.nonce,
            spec_version=self.chain.runtime["specVersion"],
            transaction_version=1,
            era={"period": period, "current": self.chain.head},
            era_block_hash=self.chain.block_hash(self.chain.head),
            call_data=call,
            payload=call,
        )

    async def rpc_request(self, method, params):
        if method == "account_nextIndex":
            return self.chain.nonce
        assert method == "state_getRuntimeVersion"
        return dict(self.chain.runtime)

    async def attach_signature(self, unsigned, signature):
        data = unsigned.call_data + bytes([unsigned.nonce]) + signature
        return SimpleNamespace(data=data, extrinsic_hash=transaction_hash(data))

    async def submit_signed(self, signed, wallet, **kwargs):
        assert kwargs == {"wait_for_inclusion": True, "wait_for_finalization": True}
        self.sent.append(bytes(signed.data))
        if self.lose_reply:
            raise TimeoutError("lost reply")
        self.chain.nonce += 1
        return "accepted"


def test_alternating_clients_use_fresh_nonce_and_send_identical_persisted_bytes(
    tmp_path,
):
    chain = Chain()
    clients = [Client(chain), Client(chain)]
    wallet = Wallet()
    for index, client in enumerate([clients[0], clients[1], clients[0]]):
        transport = SDKPreparedTransport(client, wallet)
        prepared = transport.prepare(b"weight-call", chain)
        intent = PublishIntent(tmp_path / f"intent-{index}.json")
        intent.open(
            day=1,
            height=99,
            hotkey="validator",
            netuid=82,
            intent_id=f"00000000-0000-0000-0000-{index:012x}",
        )
        intent.persist_prepared(prepared.evidence)
        marker = intent.load()
        sidecar = (
            tmp_path / "signed-transactions" / marker["prepared_transaction"]["sidecar"]
        )
        saved = json.loads(sidecar.read_text())
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        assert "signed_hex" not in json.dumps(marker)
        assert transport.send(prepared, chain) == "accepted"
        assert client.sent[-1].hex() == saved["signed_hex"][2:]
        assert saved["nonce"] == 215 + index
    assert clients[0].prepared == [215, 217]
    assert clients[0].cached_nonce == 214
    assert wallet.signatures == 3


@pytest.mark.parametrize("mutation", ["runtime", "era", "bytes", "nonce"])
def test_changes_between_persist_and_send_never_transmit(mutation):
    chain, wallet = Chain(), Wallet()
    client = Client(chain)
    transport = SDKPreparedTransport(client, wallet)
    prepared = transport.prepare(b"call", chain)
    if mutation == "runtime":
        chain.runtime = {"specVersion": 487, "transactionVersion": 1}
    if mutation == "era":
        chain.head = 228
    if mutation == "bytes":
        prepared.signed.data = b"changed"
    if mutation == "nonce":
        chain.nonce += 1
    with pytest.raises(ChainUnreadable):
        transport.send(prepared, chain)
    assert not client.sent


def test_pending_pool_nonce_disagreement_refuses_before_signing():
    chain, wallet = Chain(), Wallet()
    client = Client(chain)
    original = client.prepare_call

    def pending(*args, **kwargs):
        result = original(*args, **kwargs)
        result.nonce += 1
        return result

    client.prepare_call = pending
    with pytest.raises(ChainUnreadable):
        SDKPreparedTransport(client, wallet).prepare(b"call", chain)
    assert wallet.signatures == 0 and not client.sent


def test_lost_reply_retains_exact_private_bytes_and_new_process_does_not_sign(tmp_path):
    chain, wallet = Chain(), Wallet()
    client = Client(chain)
    transport = SDKPreparedTransport(client, wallet)
    prepared = transport.prepare(b"call", chain)
    intent = PublishIntent(tmp_path / "intent.json")
    intent.open(
        day=1,
        height=99,
        hotkey="validator",
        netuid=82,
        intent_id="00000000-0000-0000-0000-000000000001",
    )
    intent.persist_prepared(prepared.evidence)
    before = intent.path.read_bytes()
    client.lose_reply = True
    with pytest.raises(TimeoutError):
        transport.send(prepared, chain)
    restarted = PublishIntent(intent.path)
    assert restarted.path.read_bytes() == before
    assert restarted.load()["prepared_transaction"][
        "extrinsic_hash"
    ] == transaction_hash(client.sent[0])
    assert wallet.signatures == 1 and len(client.sent) == 1


def test_durable_bytes_hash_mismatch_refuses(tmp_path):
    intent = PublishIntent(tmp_path / "intent.json")
    intent.open(
        day=1,
        height=99,
        hotkey="validator",
        netuid=82,
        intent_id="00000000-0000-0000-0000-000000000001",
    )
    e = (
        SDKPreparedTransport(Client(Chain()), Wallet())
        .prepare(b"call", Chain())
        .evidence
    )
    e["signed_hex"] = "0xff"
    with pytest.raises(ChainUnreadable):
        intent.persist_prepared(e)
    assert intent.load().get("prepared_transaction") is None


def test_installed_sdk_nonce_preparation_bypasses_cache(monkeypatch):
    from bittensor._transport import interface
    from bittensor._transport.extrinsics import NonceCache
    from .test_proxy_route import SIGNER

    class RPC:
        next_nonce = 215

        async def request(self, method, params):
            return self.next_nonce

    rpc = RPC()
    cache = NonceCache(rpc)
    assert asyncio.run(cache.next_for(SIGNER)) == 215
    rpc.next_nonce = 217

    async def codec_at(_):
        return object()

    async def era(_):
        return {"period": 128, "current": 100}, "hash"

    async def genesis():
        return "genesis"

    facade = SimpleNamespace(
        _nonces=cache,
        _runtimes=SimpleNamespace(codec_at=codec_at),
        _normalize_era=era,
        genesis_hash=genesis,
        ss58_format=42,
    )
    monkeypatch.setattr(interface, "_prepare_extrinsic", lambda *a, **kw: kw)
    prepared = asyncio.run(
        interface.SubstrateConnection.prepare_unsigned(
            facade, b"call", address=SIGNER, era={"period": 128}
        )
    )
    assert prepared["nonce"] == 217
    assert asyncio.run(cache.next_for(SIGNER)) == 216  # reproduces old defect


def test_pinned_sdk_bridge_contract_without_connecting():
    import bittensor

    client = bittensor.Subtensor(network="ws://127.0.0.1:1")
    assert callable(client.prepare_call) and callable(client._call)
    assert callable(client._client._executor._enforce_raw_call)
    substrate = client._client._substrate
    assert callable(substrate.submit_signed)
    assert substrate._substrate is None  # no connection/no keys used


@pytest.mark.parametrize("kind", ["symlink", "permissive"])
def test_sidecar_directory_must_be_owned_private_real_directory(tmp_path, kind):
    intent = PublishIntent(tmp_path / "intent.json")
    intent.open(
        day=1,
        height=99,
        hotkey="validator",
        netuid=82,
        intent_id="00000000-0000-0000-0000-000000000001",
    )
    directory = tmp_path / "signed-transactions"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    if kind == "symlink":
        directory.symlink_to(elsewhere, target_is_directory=True)
    else:
        directory.mkdir(mode=0o755)
    evidence = (
        SDKPreparedTransport(Client(Chain()), Wallet())
        .prepare(b"call", Chain())
        .evidence
    )
    with pytest.raises((OSError, ChainUnreadable)):
        intent.persist_prepared(evidence)
    assert not list(elsewhere.iterdir())
    assert intent.load().get("prepared_transaction") is None


@pytest.mark.parametrize("field", ["specVersion", "transactionVersion"])
def test_finalized_runtime_upgrade_refuses_even_if_submit_endpoint_lags(field):
    chain = Chain()
    client, wallet = Client(chain), Wallet()
    transport = SDKPreparedTransport(client, wallet)
    prepared = transport.prepare(b"call", chain)
    prior = dict(chain.runtime)
    transport._live_runtime = lambda: prior
    chain.runtime = {**prior, field: prior[field] + 1}
    with pytest.raises(ChainUnreadable, match="finalized runtime changed"):
        transport.send(prepared, chain)
    assert client.sent == []
