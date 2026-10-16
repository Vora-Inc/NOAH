"""Helper exit behaviour, user-facing disclosure wording, and locked-collateral wording."""

import subprocess
import sys
import textwrap

import pytest

from noah.deposits.assistant import cli
from noah.deposits.assistant import reconcile as R
from noah.deposits.assistant import session as S
from noah.deposits.assistant.amounts import DepositRefused
from noah.deposits.assistant.config import load_config

from .fakes import ARK, COLD, MINER, OTHER_VAL, VAL, FakeChain, FakeSource, N
from .test_cli import factory, write
from .test_session import Rig

# --- exit behaviour -------------------------------------------------------------------------


def test_connection_closed_after_every_command_including_when_a_read_raised(tmp_path):
    closed = []

    class Conn(FakeChain):
        def close(self):
            closed.append(1)

    def build(config):
        return factory(tmp_path, Conn())(config)

    for command in ("preview", "status"):
        assert cli.main(["--config", write(tmp_path), command], build) == 0
    assert len(closed) == 2

    def broken_build(config):
        services = factory(tmp_path, Conn())(config)

        def boom(**kwargs):
            raise ValueError("decode failed")

        services.source.stake_info = boom
        return services

    with pytest.raises(ValueError):
        cli.main(["--config", write(tmp_path), "preview"], broken_build)
    assert len(closed) == 3


@pytest.mark.parametrize("command", ["preview", "status"])
def test_process_exits_promptly_when_a_library_leaves_a_non_daemon_thread(tmp_path, command):
    # A real subprocess through the module's own entry point. The fake services leave a
    # non-daemon thread that never ends, as a chain client's thread can. Without the fix the
    # process never exits (this times out); with it, it exits with the command's own code.
    cfg = write(tmp_path)
    script = textwrap.dedent(f"""
        import runpy, sys, threading
        from pathlib import Path
        from noah.deposits.assistant import cli
        from noah.deposits.assistant import ledger as L
        from noah.deposits.assistant.evidence import JsonFileEvidence
        from noah.deposits.assistant.session import Services
        from noah.deposits.assistant.tests.fakes import FakeChain, FakeKeypair, FakeSource

        def build(config):
            threading.Thread(target=threading.Event().wait).start()  # never ends
            return Services(
                connection=FakeChain(), source=FakeSource(),
                evidence_provider=JsonFileEvidence(config.evidence_file),
                ledger=L.Ledger(Path({str(tmp_path)!r}) / "sub.db"),
                signer_factory=FakeKeypair, ask=lambda p: "",
            )

        cli.build_services = build
        sys.argv = ["noah-deposit", "--config", {cfg!r}, {command!r}]
        runpy.run_module("noah.deposits.assistant", run_name="__main__")
    """)
    done = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr


def test_process_exit_code_is_kept_when_a_chain_read_raised(tmp_path):
    cfg = write(tmp_path)
    script = textwrap.dedent(f"""
        import runpy, sys, threading
        from noah.deposits.assistant import cli

        def build(config):
            threading.Thread(target=threading.Event().wait).start()
            raise ValueError("decode failed")

        cli.build_services = build
        sys.argv = ["noah-deposit", "--config", {cfg!r}, "preview"]
        runpy.run_module("noah.deposits.assistant", run_name="__main__")
    """)
    done = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1
    assert "decode failed" in done.stderr


# --- disclosure wording ---------------------------------------------------------------------


def test_draft_wording_notice_follows_the_flag(tmp_path, monkeypatch):
    from noah.deposits import disclosures

    assert disclosures.WORDING_IS_PLACEHOLDER is False
    r = Rig(tmp_path)
    r.run()
    text = "\n".join(r.out)
    assert "Draft wording" not in text
    assert "WIZARD_SPEC" not in text and "INV-" not in text

    monkeypatch.setattr(disclosures, "WORDING_IS_PLACEHOLDER", True)
    r2 = Rig(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    r2.run()
    assert "Draft wording, not yet approved." in "\n".join(r2.out)
    assert disclosures.WORDING_IS_PLACEHOLDER is True  # not flipped back by the helper


def test_approval_text_has_no_internal_references(tmp_path):
    r = Rig(tmp_path)
    r.typed = ["SEND 200 NOAH"]
    r.run(phrase="SEND 200 NOAH")
    text = "\n".join(r.out)
    for internal in ("Placeholder", "not approved wording", "WIZARD_SPEC", "INV-", "E-12", "D-035"):
        assert internal not in text
    assert "Noah provides no withdrawal or refund process." in text
    assert "signs it on your computer" in text
    assert "does not repay your deposit or promise you a financial return" in text


# --- locked collateral wording --------------------------------------------------------------


class LockedSource(FakeSource):
    def __init__(self, stake, locked, available):
        super().__init__(stake=stake, available=available)
        self.locked = locked

    def collateral_hotkeys(self, *, netuid, coldkey):
        return [MINER]

    def coldkey_collateral(self, *, netuid, coldkey):
        return self.locked

    def miner_collateral(self, *, netuid, hotkey, coldkey):
        return {"locked": self.locked}


def test_all_locked_stake_is_explained(tmp_path):
    # Measured on the private test chain: uid 202, 40.363198987 NOAH all locked, available 0.
    stake = 40_363_198_987
    r = Rig(tmp_path, source=LockedSource(stake=stake, locked=stake, available=0))
    res = S.preview(r.config, r.services())
    assert res["status"] == "refused" and res["code"] == "insufficient_funds"
    why = res["reason"]
    assert "locked as registration collateral cannot be used for a deposit" in why
    assert "Total stake on this miner: 40.363198987 NOAH" in why
    assert "Locked as registration collateral: 40.363198987 NOAH" in why
    assert "Available to deposit: 0 NOAH" in why
    assert "plus the 1 NOAH reserve" in why and "TAO" in why


def test_partly_locked_stake_shows_all_three_figures(tmp_path):
    src = LockedSource(stake=10 * N, locked=9500 * N // 1000, available=10 * N)
    r = Rig(tmp_path, source=src)  # 0.5 NOAH spendable, reserve 1 NOAH
    res = S.preview(r.config, r.services())
    assert res["code"] == "insufficient_funds"
    why = res["reason"]
    assert "Total stake on this miner: 10 NOAH" in why
    assert "Locked as registration collateral: 9.5 NOAH" in why
    assert "Available to deposit: 0.5 NOAH" in why


def _config_with_validator(tmp_path, validator, network=None):
    path = write(tmp_path)
    text = (
        open(path)
        .read()
        .replace(
            f"designated_validator_hotkey = {VAL}", f"designated_validator_hotkey = {validator}"
        )
    )
    if network:
        text = text.replace("[miner]\n", f"[miner]\nnetwork = {network}\n", 1)
    open(path, "w").write(text)
    return path


def test_validator_differing_from_the_launch_environment_is_refused(tmp_path):
    path = _config_with_validator(tmp_path, OTHER_VAL)
    with pytest.raises(DepositRefused) as refused:
        load_config(path, environ={"NOAH_VALIDATOR_HOTKEY": VAL})
    assert refused.value.code == "wrong_validator"
    text = str(refused.value)
    assert "earns no mining credit" in text and "Nothing was sent" in text
    same = _config_with_validator(tmp_path, VAL)
    assert load_config(same, environ={"NOAH_VALIDATOR_HOTKEY": VAL}).validator_hotkey == VAL


def test_validator_differing_from_the_mainnet_record_is_refused(tmp_path):
    from noah.core.mainnet import ARK_VALIDATOR_HOTKEY

    path = _config_with_validator(tmp_path, VAL, network="mainnet")
    with pytest.raises(DepositRefused) as refused:
        load_config(path, environ={})
    assert ARK_VALIDATOR_HOTKEY in str(refused.value)
    # a private test network has no such record, so its own validator is accepted
    assert load_config(_config_with_validator(tmp_path, VAL, "private-test"), environ={})


@pytest.mark.parametrize("command", ["preview", "run-once"])
def test_commands_stop_and_send_nothing_on_the_wrong_validator(
    tmp_path, capsys, monkeypatch, command
):
    monkeypatch.setenv("NOAH_VALIDATOR_HOTKEY", VAL)
    chain = FakeChain()
    path = _config_with_validator(tmp_path, OTHER_VAL)
    assert cli.main(["--config", path, command], factory(tmp_path, chain)) == 2
    assert "Nothing was sent" in capsys.readouterr().out
    assert chain.submitted == []


def test_read_back_does_not_qualify_a_deposit_to_a_different_validator(monkeypatch):
    # The reader is handed the configured validator and reports a deposit staked elsewhere
    # as not qualifying, with its reason; find_deposit must pass that through, never accept it.
    reason = f"staked to {OTHER_VAL}, not the designated Ark Validator {VAL} -- does not qualify"
    wrong = type(
        "D",
        (),
        dict(coldkey=COLD, origin_hotkey=MINER, token_netuid=2, amount_rao=5 * N, unattributed_reason=reason),
    )()
    monkeypatch.setattr(R, "deposits_in", lambda *a, **k: [wrong])
    match = R.find_deposit(
        None,
        expected_netuid=2,
        ark_coldkey=ARK,
        validator_hotkey=VAL,
        coldkey=COLD,
        miner_hotkey=MINER,
        amount_rao=5 * N,
        from_block=1,
        to_block=1,
    )
    assert match.qualifies is False and "not the designated Ark Validator" in match.reason


class GenesisChain(FakeChain):
    def __init__(self, genesis):
        super().__init__()
        self.genesis = genesis

    def get_block_hash(self, n):
        return self.genesis if n == 0 else super().get_block_hash(n)


def test_mainnet_genesis_requires_the_designated_validator_whatever_the_network_text(tmp_path):
    from noah.core.chain import MAINNET

    # network text says private-test (the fakes' default), the typed validator is wrong
    r = Rig(tmp_path, chain=GenesisChain(MAINNET), validator_hotkey=OTHER_VAL)
    assert r.config.network == "private-test"
    res = S.preview(r.config, r.services())
    assert res["status"] == "refused" and res["code"] == "wrong_validator"
    assert "live mainnet" in res["reason"] and "Nothing was sent" in res["reason"]
    assert r.run()["code"] == "wrong_validator"
    with pytest.raises(DepositRefused):
        S.resolve_credit(r.config, r.services(), "abc", 1)
    # status, through the command
    path = _config_with_validator(tmp_path, OTHER_VAL)
    assert cli.main(["--config", path, "status"], lambda config: r.services()) == 2
    assert r.chain.submitted == [] and r.signer_calls == 0


def test_mainnet_genesis_with_the_designated_validator_is_not_refused_by_the_guard(tmp_path):
    from noah.core.chain import MAINNET
    from noah.core.mainnet import ARK_VALIDATOR_HOTKEY

    r = Rig(tmp_path, chain=GenesisChain(MAINNET), validator_hotkey=ARK_VALIDATOR_HOTKEY)
    assert S.preview(r.config, r.services()).get("code") != "wrong_validator"


def test_non_mainnet_genesis_is_unaffected(tmp_path):
    r = Rig(tmp_path, chain=GenesisChain("0x" + "ab" * 32), validator_hotkey=OTHER_VAL)
    assert S.preview(r.config, r.services())["status"] == "ready"
    S.reconcile_open(r.config, r.services())


def test_no_locked_collateral_keeps_the_plain_message(tmp_path):
    r = Rig(tmp_path, source=FakeSource(stake=0))
    assert S.preview(r.config, r.services())["reason"] == (
        "Nothing can be sent: 0 NOAH is available and 1 NOAH is held back as your reserve."
    )
