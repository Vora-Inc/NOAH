import json

from noah.deposits.assistant import cli
from noah.deposits.assistant import ledger as L
from noah.deposits.assistant.config import EXAMPLE
from noah.deposits.assistant.evidence import JsonFileEvidence
from noah.deposits.assistant.session import Services

from .fakes import ARK, COLD, MINER, VAL, FakeChain, FakeKeypair, FakeSource, N


def write(tmp_path, mode="maintenance"):
    ev = tmp_path / "e.json"
    ev.write_text(
        json.dumps(
            {
                "through_day": "2026-09-29",
                "history_complete": True,
                "cumulative_eligible_earnings_rao": 1000 * N,
                "credited_contributions_rao": 300 * N,
                "period_eligible_earnings_rao": 100 * N,
                "period_start_day": "2026-09-28",
            }
        )
    )
    cfg = tmp_path / "c.ini"
    cfg.write_text(f"""[miner]
endpoint = ws://node.example:9944
netuid = 2
miner_hotkey = {MINER}
coldkey = {COLD}
wallet_name = w
hotkey_name = h
[ark]
ark_coldkey = {ARK}
designated_validator_hotkey = {VAL}
[deposit]
mode = {mode}
reserve_noah = 1
[evidence]
file = {ev}
""")
    return str(cfg)


def factory(tmp_path, chain=None):
    def build(config):
        return Services(
            connection=chain or FakeChain(),
            source=FakeSource(),
            evidence_provider=JsonFileEvidence(config.evidence_file),
            ledger=L.Ledger(tmp_path / "cli.db"),
            signer_factory=FakeKeypair,
            ask=lambda prompt: "SEND 200 NOAH",
        )

    return build


def test_init_config_writes_once_and_never_overwrites(tmp_path, capsys):
    target = str(tmp_path / "x.ini")
    assert cli.main(["--config", target, "init-config"]) == 0
    assert open(target).read() == EXAMPLE
    assert cli.main(["--config", target, "init-config"]) == 1


def test_preview_command(tmp_path, capsys):
    assert cli.main(["--config", write(tmp_path), "preview"], factory(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "a preview sends nothing" in out and "200 NOAH" in out


def test_run_once_then_status(tmp_path, capsys):
    chain = FakeChain()
    assert cli.main(["--config", write(tmp_path), "run-once"], factory(tmp_path, chain)) == 0
    assert len(chain.submitted) == 1
    assert cli.main(["--config", write(tmp_path), "status"], factory(tmp_path, chain)) == 0
    assert "finalized" in capsys.readouterr().out


def test_missing_validator_stops_with_plain_reason(tmp_path, capsys):
    path = write(tmp_path)
    text = open(path).read().replace(f"designated_validator_hotkey = {VAL}\n", "")
    open(path, "w").write(text)
    assert cli.main(["--config", path, "preview"], factory(tmp_path)) == 2
    assert "Ark Validator" in capsys.readouterr().out


import pytest  # noqa: E402

from noah.deposits.assistant.config import load_config  # noqa: E402


@pytest.mark.parametrize("error", [ConnectionRefusedError, OSError, TimeoutError])
@pytest.mark.parametrize("command", ["preview", "run-once", "status"])
def test_unreachable_node_at_construction_is_a_plain_refusal(tmp_path, capsys, command, error):
    def build(config):
        raise error("connect failed")

    assert cli.main(["--config", write(tmp_path), command], build) == 2
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "ws://node.example:9944" in out and error.__name__ in out
    assert "Check that the node is running" in out


@pytest.mark.parametrize("error", [ConnectionError, OSError, TimeoutError])
@pytest.mark.parametrize("command", ["preview", "run-once"])
def test_unreachable_node_at_read_time_is_a_plain_refusal(tmp_path, capsys, command, error):
    def build(config):
        services = factory(tmp_path)(config)

        def broken(**kwargs):
            raise error("socket dropped")

        services.source.stake_info = broken
        return services

    assert cli.main(["--config", write(tmp_path), command], build) == 2
    out = capsys.readouterr().out
    assert "Traceback" not in out and "ws://node.example:9944" in out
    assert "run `status`" in out


def test_follow_up_check_failure_after_sending_keeps_the_deposit_recorded(tmp_path, capsys):
    chain = FakeChain()
    real = chain.get_chain_finalised_head

    def flaky():
        if chain.submitted:  # reads before sending work; the check after sending drops
            raise ConnectionError("dropped")
        return real()

    chain.get_chain_finalised_head = flaky
    code = cli.main(["--config", write(tmp_path), "run-once"], factory(tmp_path, chain))
    out = capsys.readouterr().out
    assert len(chain.submitted) == 1 and "Traceback" not in out
    assert "could not reach the chain" in out and "recorded as 'submitted'" in out
    assert code == 0  # the deposit itself was sent and is recorded; only the check failed


def test_unrelated_errors_are_not_disguised(tmp_path):
    def build(config):
        raise ValueError("a real bug")

    with pytest.raises(ValueError):
        cli.main(["--config", write(tmp_path), "preview"], build)


def test_init_config_output_and_file_explain_the_endpoint(tmp_path, capsys):
    target = str(tmp_path / "x.ini")
    cli.main(["--config", target, "init-config"])
    out = capsys.readouterr().out
    assert "ws://127.0.0.1:9944" in out and "trust" in out and "genesis_hash" in out
    text = open(target).read()
    assert "node running on THIS computer" in text and "refuses to run" in text


def test_wallet_path_is_a_supported_optional_key(tmp_path):
    from noah.deposits.assistant.config import EXAMPLE

    assert "# wallet_path =" in EXAMPLE and "~/.bittensor/wallets" in EXAMPLE
    path = write(tmp_path)
    assert load_config(path, environ={}).wallet_path is None
    text = (
        open(path)
        .read()
        .replace("wallet_name = w\n", "wallet_name = w\nwallet_path = /x/wallets\n")
    )
    open(path, "w").write(text)
    assert load_config(path, environ={}).wallet_path == "/x/wallets"
