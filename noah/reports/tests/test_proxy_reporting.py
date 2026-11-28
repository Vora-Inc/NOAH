"""Proxy report attribution: public identities, synthetic events, no real signing."""

import copy
from types import SimpleNamespace as NS

import pytest

from noah.reports.backing.chain_session import commitment_info
from noah.reports.backing.collector import _decode_block
from noah.reports.backing.keys import ss58_encode
from noah.reports.tests.test_miner_commands import case as case

REAL = ss58_encode(bytes.fromhex("33" * 32))
DELEGATE = ss58_encode(bytes.fromhex("55" * 32))
OTHER = ss58_encode(bytes.fromhex("66" * 32))


def event(module, name, attrs, index=1):
    return {
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": index,
        "event": {"module_id": module, "event_id": name, "attributes": attrs},
    }


def fixture():
    inner = {
        "call_module": "Commitments",
        "call_function": "set_commitment",
        "call_args": [
            {"name": "netuid", "value": 82},
            {"name": "info", "value": commitment_info("Raw32", bytes(32))},
        ],
    }
    value = {
        "address": DELEGATE,
        "call": {
            "call_module": "Proxy",
            "call_function": "proxy",
            "call_args": [
                {"name": "real", "value": REAL},
                {"name": "force_proxy_type", "value": "Validate"},
                {"name": "call", "value": inner},
            ],
        },
    }
    events = [
        event("Commitments", "Commitment", {"netuid": 82, "who": REAL}),
        event("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
        event("System", "ExtrinsicSuccess", {}),
    ]
    return value, events


def decode(value, events):
    sdk = NS(
        rpc_request=lambda *_: {"result": {"block": {}}},
        get_block=lambda **_: {"extrinsics": [NS(value={}), NS(value=value)]},
        query=lambda *_args, **_kwargs: NS(value=events),
    )
    row = {
        "hash": "0x" + "aa" * 32,
        "number": 100,
        "timestamp_ms": 123000,
        "finalized": True,
        "finality": {"matches": True},
    }
    return _decode_block(sdk, row, mining_netuid=82)[0]


def test_successful_proxy_report_credits_real_hotkey_not_delegate():
    value, events = fixture()
    rows = decode(value, events)
    assert len(rows) == 1
    assert rows[0]["signer"] == "33" * 32
    assert rows[0]["proxy_delegate"] == "55" * 32
    assert rows[0]["successful"] is True
    assert rows[0]["data_hex"] == "00" * 32


@pytest.mark.parametrize(
    "mutation",
    [
        "inner-failed",
        "missing-proxy-result",
        "duplicate-proxy-result",
        "wrong-who",
        "wrong-netuid",
        "wrong-event-index",
        "outer-failed",
        "wrong-pallet",
        "missing-commitment",
    ],
)
def test_proxy_outer_success_never_credits_failed_or_unbound_inner_call(mutation):
    value, events = fixture()
    if mutation == "inner-failed":
        events[1]["event"]["attributes"]["result"] = {"Err": {"Module": {}}}
    elif mutation == "missing-proxy-result":
        events.pop(1)
    elif mutation == "duplicate-proxy-result":
        events.append(copy.deepcopy(events[1]))
    elif mutation == "wrong-who":
        events[0]["event"]["attributes"]["who"] = OTHER
    elif mutation == "wrong-netuid":
        events[0]["event"]["attributes"]["netuid"] = 7
    elif mutation == "wrong-event-index":
        events[0]["extrinsic_idx"] = 2
    elif mutation == "outer-failed":
        events[2]["event"]["event_id"] = "ExtrinsicFailed"
    elif mutation == "wrong-pallet":
        events[1]["event"]["module_id"] = "Other"
    elif mutation == "missing-commitment":
        events.pop(0)
    assert not any(x["successful"] for x in decode(value, events))


def test_proxy_does_not_enable_arbitrary_wrappers_or_broader_grants():
    value, events = fixture()
    for broad in ["Any", None, "NonTransfer"]:
        changed = copy.deepcopy(value)
        changed["call"]["call_args"][1]["value"] = broad
        assert decode(changed, events) == []
    changed = {
        "address": DELEGATE,
        "call": {
            "call_module": "Utility",
            "call_function": "batch_all",
            "call_args": [{"name": "calls", "value": [value["call"]]}],
        },
    }
    assert decode(changed, events) == []


def test_all_18_ledger_hotkey_identities_stay_separate_under_one_proxy():
    # Public-key identities only; cryptographic signing is tested separately.
    seen = set()
    for n in range(1, 19):
        value, events = fixture()
        public = bytes([n]) * 32
        value["call"]["call_args"][0]["value"] = ss58_encode(public)
        events[0]["event"]["attributes"]["who"] = ss58_encode(public)
        rows = decode(value, events)
        assert len(rows) == 1 and rows[0]["signer"] == public.hex()
        seen.add(rows[0]["signer"])
    assert len(seen) == 18


def setup_proxy_case(case):
    from noah.reports import miner
    from noah.reports.backing.protocol.preimage import REPORT_POLICY

    c, r, chain, engine = case
    c.report_proxy_address = DELEGATE
    r["eligibility_policy_version"] = REPORT_POLICY
    miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    return c, r, chain


def test_proxy_sender_uses_real_registration_and_preserves_payload(case):
    from noah.reports import miner
    from noah.reports.common import state_directory
    from noah.reports.tests.test_submit import Key

    c, r, chain = setup_proxy_case(case)
    original = (state_directory(c, r) / "prepared.json").read_bytes()
    result = miner.submit_tick(
        c, r, chain=chain, authorized=True, signer_factory=lambda _: Key(bytes.fromhex("55" * 32))
    )
    assert result["commit"] == "finalized"
    chain.now = r["reveal_open_ms"]
    result = miner.submit_tick(
        c, r, chain=chain, authorized=True, signer_factory=lambda _: Key(bytes.fromhex("55" * 32))
    )
    assert result["reveal"] == "finalized"
    assert (state_directory(c, r) / "prepared.json").read_bytes() == original
    assert len(chain.sent) == 2


def test_unknown_proxy_submission_recovers_without_signer(case):
    from noah.reports import miner
    from noah.reports.backing.submit import SubmitError
    from noah.reports.tests.test_submit import Key

    c, r, chain = setup_proxy_case(case)
    broadcast = chain.broadcast

    def lost(handle):
        broadcast(handle)
        raise ConnectionError("lost reply")

    chain.broadcast = lost
    with pytest.raises(SubmitError, match="unknown"):
        miner.submit_tick(
            c,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: Key(bytes.fromhex("55" * 32)),
        )
    chain.broadcast = broadcast

    def forbidden(_):
        raise AssertionError("recovery must not open a wallet")

    result = miner.submit_tick(c, r, chain=chain, signer_factory=forbidden)
    assert result["commit"] == "finalized" and result["sent"] is False
    assert len(chain.sent) == 1


def test_shared_proxy_pending_nonce_blocks_another_miner(case):
    from noah.reports import miner
    from noah.reports.backing.submit import SubmitError
    from noah.reports.common import ReportError
    from noah.reports.tests.test_submit import Key

    c, r, chain = setup_proxy_case(case)

    def unknown(_):
        raise ConnectionError("lost reply")

    chain.broadcast = unknown
    with pytest.raises(SubmitError):
        miner.submit_tick(
            c,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: Key(bytes.fromhex("55" * 32)),
        )
    other = copy.copy(c)
    other.miner_hotkey = OTHER
    chain.registered.add((82, OTHER))
    engine = case[3]
    miner.prepare_report(other, r, compute_fn=lambda *_: engine)
    with pytest.raises(ReportError, match="unfinished proxy"):
        miner.submit_tick(
            other,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: pytest.fail("must not unlock"),
        )


def test_report_proxy_config_is_separate_from_deposit_proxy(tmp_path):
    from noah.deposits.assistant.config import load_config
    from noah.deposits.assistant.tests.test_assignment_trust_config import configured

    path = configured(tmp_path, "proxy_address = " + DELEGATE + "\n")
    config = load_config(path, environ={})
    assert config.report_proxy_address == DELEGATE
    assert config.signing == {"mode": "direct"}


def proxy_adapter():
    from noah.reports.tests.test_signing_runtime_guard import adapter

    chain, composed = adapter(), []
    chain.report_proxy = {"real": REAL, "delegate": DELEGATE, "netuid": 82}
    sdk = chain.substrate
    sdk.grants = [{"delegate": DELEGATE, "proxy_type": "Validate", "delay": 0}]
    sdk.free = 10000
    sdk.query = lambda pallet, name, params, **kw: NS(
        value=(sdk.grants, 0)
        if (pallet, name) == ("Proxy", "Proxies")
        else {"data": {"free": sdk.free, "frozen": 0}}
    )
    sdk.get_constant = lambda *_args, **_kwargs: NS(value=1)
    sdk.get_payment_info = lambda **_: {"partialFee": 100}
    compose = sdk.compose_call

    def record(*args, **kwargs):
        composed.append((args, kwargs))
        return compose(*args, **kwargs)

    sdk.compose_call = record
    return chain, composed


def proxy_key(callback):
    return NS(
        crypto_type=1, public_key=bytes.fromhex("55" * 32), ss58_address=DELEGATE, sign=callback
    )


def test_real_adapter_wraps_only_validate_and_signs_once_as_delegate():
    chain, calls = proxy_adapter()
    signed = []
    chain.sign(proxy_key(lambda p: signed.append(p) or bytes(64)), 82, "Raw32", bytes(32))
    assert len(signed) == 1
    assert len(calls) == 2
    args, kw = calls[1]
    assert args[:2] == ("Proxy", "proxy")
    assert args[2]["real"] == REAL and args[2]["force_proxy_type"] == "Validate"


@pytest.mark.parametrize("problem", ["revoked", "delayed", "any", "wrong-delegate", "no-fees"])
def test_proxy_preflight_refuses_before_any_signature(problem):
    from noah.reports.backing.submit import SigningRuntimeRefused

    chain, _ = proxy_adapter()
    if problem == "revoked":
        chain.substrate.grants = []
    if problem == "delayed":
        chain.substrate.grants[0]["delay"] = 1
    if problem == "any":
        chain.substrate.grants[0]["proxy_type"] = "Any"
    if problem == "wrong-delegate":
        chain.substrate.grants[0]["delegate"] = OTHER
    if problem == "no-fees":
        chain.substrate.free = 100
    with pytest.raises(SigningRuntimeRefused):
        chain.sign(proxy_key(lambda _: pytest.fail("no signature allowed")), 82, "Raw32", bytes(32))


@pytest.mark.parametrize("failure", ["inner-failed", "wrong-real", "wrong-delegate"])
def test_recovery_adapter_rechecks_nested_result_and_both_identities(failure):
    from noah.reports.backing.chain_session import SubstrateChain

    value, events = fixture()
    chain = SubstrateChain.__new__(SubstrateChain)
    chain.report_proxy = {"real": REAL, "delegate": DELEGATE, "netuid": 82}
    chain.block_events = lambda _: events
    chain.substrate = NS(
        get_block_header=lambda _: {"header": {"number": 100}},
        get_block=lambda **_: {"extrinsics": [NS(value={}), NS(value=value)]},
        query=lambda *_a, **_k: NS(value=123000),
    )
    assert chain._inclusion_from_block("0x" + "aa" * 32, 1, "0x" + "bb" * 32).success
    if failure == "inner-failed":
        events[1]["event"]["attributes"]["result"] = {"Err": "CallFiltered"}
    elif failure == "wrong-real":
        value["call"]["call_args"][0]["value"] = OTHER
    else:
        value["address"] = OTHER
    assert not chain._inclusion_from_block("0x" + "aa" * 32, 1, "0x" + "bb" * 32).success


def test_direct_commit_cannot_silently_switch_to_proxy_for_reveal(case):
    from noah.reports import miner
    from noah.reports.backing.protocol.preimage import REPORT_POLICY
    from noah.reports.common import ReportError
    from noah.reports.tests.test_submit import Key

    c, r, chain, engine = case
    r["eligibility_policy_version"] = REPORT_POLICY
    miner.prepare_report(c, r, compute_fn=lambda *_: engine)
    miner.submit_tick(
        c, r, chain=chain, authorized=True, signer_factory=lambda _: Key(bytes.fromhex("33" * 32))
    )
    c.report_proxy_address = DELEGATE
    chain.now = r["reveal_open_ms"]
    with pytest.raises(ReportError, match="original direct config"):
        miner.submit_tick(
            c,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: pytest.fail("must not sign"),
        )


def test_concurrent_process_cannot_enter_shared_sender_lane(case):
    from noah.reports.common import ReportError, state_directory
    from noah.reports.proxy import sender_lane

    c, r, chain = setup_proxy_case(case)
    directory = state_directory(c, r)
    with sender_lane(c, directory):
        with pytest.raises(ReportError):
            with sender_lane(c, directory):
                pytest.fail("second sender acquired lock")


def test_corrupt_shared_sender_record_refuses_without_wallet(case):
    from noah.reports.common import ReportError, state_directory
    from noah.reports.proxy import _lane, sender_lane

    c, r, chain = setup_proxy_case(case)
    path = _lane(c)
    path.mkdir(parents=True)
    (path / "active.json").write_text("{}")
    with pytest.raises(ReportError, match="invalid proxy sender"):
        with sender_lane(c, state_directory(c, r)):
            pytest.fail("invalid sender record accepted")


def test_public_proxy_fee_check_is_retryable_and_never_signs():
    from noah.reports.backing.submit import SigningRuntimeRefused

    chain, calls = proxy_adapter()
    chain.substrate.free = 0
    with pytest.raises(SigningRuntimeRefused):
        chain.preflight_proxy_report(82, "Raw32", bytes(32))
    chain.substrate.free = 10000
    chain.preflight_proxy_report(82, "Raw32", bytes(32))
    assert len(calls) == 4


def _next_day(contract):
    """Same snapshot, with commit and reveal one UTC day later."""
    nxt = copy.deepcopy(contract)
    for key in ("commit_open_ms", "commit_close_ms", "reveal_open_ms", "reveal_close_ms"):
        nxt[key] += 86_400_000
    nxt["release_prefix"] = "aa" * 16
    return nxt


def test_marker_without_journal_is_cleared_and_the_next_day_proceeds(case):
    """A crash after the sender marker and before the phase journal signed nothing."""
    from noah.reports import miner
    from noah.reports.common import read_json, state_directory
    from noah.reports.proxy import _lane, claim
    from noah.reports.tests.test_submit import Key

    c, r, chain = setup_proxy_case(case)
    directory = state_directory(c, r)
    _lane(c).mkdir(parents=True)
    claim(c, directory, "commit")
    assert (_lane(c) / "active.json").is_file()
    assert not (directory / "commit.journal").exists()
    chain.now = r["commit_close_ms"]
    expired = miner.submit_tick(
        c, r, chain=chain, authorized=True, signer_factory=lambda _: pytest.fail("must not sign")
    )
    assert expired["action"] == "expired" and expired["sent"] is False
    assert not (_lane(c) / "active.json").exists()
    assert chain.sent == []

    nxt = _next_day(r)
    chain.now = nxt["commit_open_ms"]
    engine = case[3]
    signs = []

    def delegate(_config):
        signs.append("signed")
        return Key(bytes.fromhex("55" * 32))

    miner.prepare_report(c, nxt, compute_fn=lambda *_: engine)
    original = chain.sign

    def watch(keypair, netuid, variant, payload):
        journal = read_json(state_directory(c, nxt) / "commit.journal")
        assert journal["state"] == "claimed" and journal["extrinsic_hash"] is None
        assert read_json(_lane(c) / "active.json")["directory"] == str(
            state_directory(c, nxt).resolve()
        )
        return original(keypair, netuid, variant, payload)

    chain.sign = watch
    first = miner.submit_tick(c, nxt, chain=chain, authorized=True, signer_factory=delegate)
    chain.sign = original
    assert first["commit"] == "finalized" and first["sent"] is True
    other = copy.copy(c)
    other.miner_hotkey = OTHER
    chain.registered.add((82, OTHER))
    miner.prepare_report(other, nxt, compute_fn=lambda *_: engine)
    second = miner.submit_tick(other, nxt, chain=chain, authorized=True, signer_factory=delegate)
    assert second["commit"] == "finalized" and second["sent"] is True
    assert signs == ["signed", "signed"]
    assert len(chain.sent) == 2


def test_crash_after_broadcast_still_blocks_and_recovers_without_a_second_signature(case):
    from noah.reports import miner
    from noah.reports.backing.submit import SubmitError
    from noah.reports.common import ReportError, read_json, state_directory
    from noah.reports.proxy import _lane
    from noah.reports.tests.test_submit import Key

    c, r, chain = setup_proxy_case(case)
    broadcast = chain.broadcast

    def lost(handle):
        broadcast(handle)
        raise ConnectionError("lost reply")

    chain.broadcast = lost
    with pytest.raises(SubmitError, match="unknown"):
        miner.submit_tick(
            c,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: Key(bytes.fromhex("55" * 32)),
        )
    journal = read_json(state_directory(c, r) / "commit.journal")
    assert journal["state"] == "unresolved" and journal["extrinsic_hash"]
    marker = read_json(_lane(c) / "active.json")
    assert marker["phase"] == "commit"
    assert marker["directory"] == str(state_directory(c, r).resolve())
    other = copy.copy(c)
    other.miner_hotkey = OTHER
    chain.registered.add((82, OTHER))
    miner.prepare_report(other, r, compute_fn=lambda *_: case[3])
    with pytest.raises(ReportError, match="unfinished proxy"):
        miner.submit_tick(
            other,
            r,
            chain=chain,
            authorized=True,
            signer_factory=lambda _: pytest.fail("must not sign"),
        )
    chain.broadcast = broadcast
    recovered = miner.submit_tick(
        c, r, chain=chain, signer_factory=lambda _: pytest.fail("must not sign")
    )
    assert recovered["commit"] == "finalized" and recovered["sent"] is False
    assert len(chain.sent) == 1
    followed = miner.submit_tick(
        other,
        r,
        chain=chain,
        authorized=True,
        signer_factory=lambda _: Key(bytes.fromhex("55" * 32)),
    )
    assert followed["commit"] == "finalized" and followed["sent"] is True
    assert len(chain.sent) == 2
