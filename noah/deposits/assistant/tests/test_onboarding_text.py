"""The wrapped-deposit statement and stake_position guidance appear where a newcomer looks."""

import re
from pathlib import Path

import pytest

from noah.deposits.assistant import cli
from noah.deposits.assistant.config import EXAMPLE, STAKE_POSITION_HELP, WRAPPED_DEPOSIT_NOTICE

NOTICE = (
    "A transfer made with transfer_stake_and_hotkey is attributed to your miner hotkey from the "
    "transfer itself, even when it is sent inside a proxy, multisig or batch. A plain transfer_stake is "
    "attributed only through a remark in a top-level batch_all; a plain transfer_stake with no "
    "remark, or one whose remark sits inside a proxy, multisig, Utility.batch or another wrapper "
    "where it is not read, names no miner hotkey, so the deposit is recorded as unattributed, "
    "with a reason, and credits nobody (the chain itself shows no error). This helper sends NOAH "
    "directly, using transfer_stake_and_hotkey (stake_position = miner) or a top-level batch_all "
    "that carries the remark (stake_position = validator), with optional proxy signing only for the miner position. The entry "
    "deposit must name your miner hotkey the first time; an unnamed attempt is recorded as "
    "unattributed and does not count as your entry."
)
OLD_BLANKET = "are not attributed to a specific miner hotkey. This helper sends NOAH directly"
USAGE = Path(cli.__file__).with_name("USAGE.md")


def flat(text):
    """Comment markers and line breaks removed, so wrapped lines compare equal."""
    text = re.sub(r"^[#>]\s?", "", text, flags=re.M)
    return " ".join(text.split())


def test_notice_wording_is_exact():
    assert WRAPPED_DEPOSIT_NOTICE == NOTICE


def test_top_level_help_has_notice_and_both_positions(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = flat(capsys.readouterr().out)
    assert NOTICE in out
    assert "stake_position = miner: your NOAH is staked to your own miner hotkey" in out
    assert "stake_position = validator: your NOAH is already staked to the designated Ark" in out
    assert "you own the miner hotkey with the same coldkey" in out


def test_init_config_output_has_notice_and_both_positions(tmp_path, capsys):
    cli.main(["--config", str(tmp_path / "x.ini"), "init-config"])
    out = flat(capsys.readouterr().out)
    assert NOTICE in out and flat(STAKE_POSITION_HELP) in out


def test_example_config_has_notice_and_both_positions():
    text = flat(EXAMPLE)
    assert NOTICE in text
    assert "miner (default): your NOAH is staked to your own miner hotkey" in text
    assert "validator: your NOAH is already staked to the designated Ark Validator" in text
    assert "own the miner hotkey with the same coldkey" in text


def test_usage_guide_has_notice_and_not_in_this_helper_list():
    text = flat(USAGE.read_text())
    assert NOTICE in text
    for needle in (
        "No proxy setup wizard",
        "No schedule",
        "No hardware-wallet path",
        "No setup wizard",
        "hand-supplied",
        "10 NOAH entry contribution is a separate requirement",
    ):
        assert needle in text


def test_usage_guide_only_names_real_commands_keys_and_flags():
    text = USAGE.read_text()
    from noah.chain.sources import QuietArgumentParser
    from noah.deposits.assistant import miner_cli
    parser = miner_cli._parser(QuietArgumentParser(add_help=False))
    commands = set(parser._subparsers._group_actions[0].choices)
    assert {'init-config', 'preview', 'deposit', 'status', 'resolve'} <= commands
    for used in re.findall(r"noah-deposit\.ini (\S+)", text):
        assert used in commands or used == "COMMAND"
    for flag in re.findall(r"(--[a-z][a-z-]+)", text):
        assert flag in {"--config", "--label", "--credited-before-rao", "--help", "--preview", "--status",
                        "--cancel-unsubmitted", "--recover-pre-submit"}, flag


def _real_keys():
    source = Path(cli.__file__).with_name("config.py").read_text()
    keys = set(re.findall(r'get\(\s*"(\w+)",\s*"(\w+)"', source))
    keys |= {("deposit", k) for k in re.findall(r'noah\("(\w+)"\)', source)}
    return keys


def test_usage_config_table_keys_match_the_real_section_and_key_names():
    real = _real_keys()
    assert ("miner", "stake_position") in real and ("deposit", "reserve_noah") in real
    section, documented = None, set()
    for line in USAGE.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        for token in re.findall(r"`([^`]+)`", first_cell):
            marked = re.fullmatch(r"\[(\w+)\] (\w+)", token)
            if marked:
                section = marked.group(1)
                token = marked.group(2)
            documented.add((section, token))
    assert documented <= real, sorted(documented - real)
    # and nothing the file really reads is left undocumented
    assert real <= documented, sorted(real - documented)


def test_reserve_default_text_matches_the_generated_example():
    assert "reserve_noah = 1" in EXAMPLE
    text = flat(USAGE.read_text())
    assert "left out, 0" in text and "sets it to 1" in text


def test_signing_line_does_not_promise_hardware_wallets():
    source = (
        Path(cli.__file__).with_name("preview.py").read_text()
        + Path(cli.__file__).with_name("session.py").read_text()
    )
    assert "Hardware wallets will ask" not in source and "hardware confirmation" not in source


def test_the_old_blanket_claim_never_reappears():
    package = Path(cli.__file__).parent
    for path in list(package.glob("*.py")) + [USAGE]:
        assert OLD_BLANKET not in flat(path.read_text()), path.name
        assert "Wrapped deposits are not attributed" not in flat(path.read_text()), path.name


def _code_strings():
    """Every string literal in the helper's code (not its tests), f-strings by their literal parts."""
    import ast

    out = []
    for path in Path(cli.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(flat(node.value))
    return out


def test_quoted_troubleshooting_strings_exist_verbatim_in_the_code():
    """A typo in a quoted string in the troubleshooting table must fail here."""
    strings = _code_strings()
    rows = [
        line
        for line in USAGE.read_text().split("## 8. Troubleshooting", 1)[1].splitlines()
        if line.startswith('| "')
    ]
    assert len(rows) >= 8
    checked = 0
    for line in rows:
        first_cell = line.split("|")[1].strip()
        for quoted in re.findall(r'"([^"]+)"', first_cell):
            for piece in quoted.split("..."):
                piece = flat(piece).strip()
                if len(piece) < 4:
                    continue
                checked += 1
                assert any(piece in text for text in strings), piece
    assert checked >= 10
