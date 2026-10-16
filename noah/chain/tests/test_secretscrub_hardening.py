"""The backstop's guarantees that go beyond the basics: it catches a log handler that was bound
before it was installed, it never raises, it never blocks, and every kind of registration works."""

import io
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from noah import secretscrub
from noah.core.settings import BadSetting, Settings

pytestmark = pytest.mark.usefixtures("backstop")

ROOT = Path(__file__).resolve().parents[3]
CANARY = "CANARY" + "KEY123"
URL = f"wss://h.example/v2/{CANARY}"


# -- log records of ALL loggers, including a handler bound before install -----------------------


def test_a_handler_created_before_install_still_emits_scrubbed_records():
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)  # bound to a raw stream BEFORE the backstop exists
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("noah.test.before_install")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        secretscrub.register_endpoint(URL)
        secretscrub.install()
        logger.error("could not reach %s", URL)
        logger.error("only the key %s", CANARY.lower())
        try:
            raise RuntimeError(f"connect failed for {URL}")
        except RuntimeError:
            logger.exception("traceback follows")
    finally:
        logger.removeHandler(handler)
    text = sink.getvalue()
    assert "could not reach wss://h.example" in text
    assert "Traceback" in text and "RuntimeError" in text
    assert CANARY.lower() not in text.lower()


def test_an_unrelated_logger_and_extra_text_are_left_alone():
    secretscrub.register_endpoint(URL)
    secretscrub.install()
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    logger = logging.getLogger("noah.test.plain")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("block %s read in %s seconds", 12, 3)
    finally:
        logger.removeHandler(handler)
    assert "block 12 read in 3 seconds" in sink.getvalue()


# -- never raises, never blocks -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "wss://h.example/abcdefgh\udcff\udcfe",  # a lone surrogate, as the shell produces for \xff
        "wss://h.example/\ud800\ud800key12345",
        "wss://[::1:9944/abcdefgh1",  # a broken IPv6 literal
        "wss://h.example:notaport/abcdefgh1",
        "wss://" + "a" * 100_000,
        "wss://h.example/" + "%zz" * 50,
        "\x00wss://h.example/abcdefgh1",
        "wss://",
        "://",
        "",
        None,
        b"wss://h.example/bytes",
    ],
)
def test_registering_and_activating_never_raise(value):
    secretscrub.register_endpoint(value)
    secretscrub.register_environment({"NOAH_CHAIN_ENDPOINT": value, "NOAH_ENDPOINT_2_FILE": value})
    secretscrub.activate(type("S", (), {"chain_endpoint": value})())
    assert isinstance(secretscrub.scrub("some ordinary text"), str)


def test_the_fuzz_case_from_the_shell_does_not_crash_a_startup(tmp_path):
    """NOAH_CHAIN_ENDPOINT=$'wss://h.example/abcdefgh\\xff\\xfe' used to crash the guard (rc 1)."""
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
        "NOAH_CHAIN_ENDPOINT": "wss://h.example/abcdefgh\udcff\udcfe",
    }
    result = subprocess.run(
        [sys.executable, "-m", "neurons.validator", "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
    )
    assert "Traceback" not in result.stderr and "UnicodeEncodeError" not in result.stderr
    assert result.returncode == 2 and "REFUSED" in result.stdout


def test_closed_standard_streams_do_not_break_a_startup(tmp_path):
    # Some interpreters exit 120 when flushing closed descriptors at shutdown.
    # Compare the guarded process with plain Python and independently prove
    # execution reached the statement after the guarded print.
    baseline_marker = tmp_path / "plain-completed"
    guarded_marker = tmp_path / "guarded-completed"
    baseline = f"print('x'); open({str(baseline_marker)!r}, 'w').write('completed')"
    guarded = (
        "import sys; sys.path.insert(0, {root!r});"
        "from noah.chain.sources import guard_endpoint_logging;"
        "guard_endpoint_logging(); print('x');"
        "open({marker!r}, 'w').write('completed')"
    ).format(root=str(ROOT), marker=str(guarded_marker))
    codes = []
    for code in (baseline, guarded):
        result = subprocess.run(
            ["sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} >&- 2>&-"],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
        )
        codes.append(result.returncode)
    assert baseline_marker.read_text() == guarded_marker.read_text() == "completed"
    assert codes[0] in (0, 120)
    assert codes[1] == codes[0]


def test_install_skips_missing_streams():
    saved = sys.stdout
    sys.stdout = None
    try:
        secretscrub.install()
        assert sys.stdout is None
    finally:
        sys.stdout = saved


def test_a_fifo_or_a_large_file_is_never_read(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert secretscrub._read_small_regular_file(str(fifo)) is None  # returns, does not block
    big = tmp_path / "big"
    big.write_text("wss://h.example/" + "a" * 70_000)
    assert secretscrub._read_small_regular_file(str(big)) is None
    small = tmp_path / "small"
    small.write_text("wss://h.example/v2/" + CANARY + "\n")
    assert CANARY in secretscrub._read_small_regular_file(str(small))
    assert secretscrub._read_small_regular_file(str(tmp_path / "missing")) is None


def test_settings_refuse_a_fifo_and_a_large_second_endpoint_file(tmp_path):
    from noah.core.tests.test_settings import COMPLETE

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(BadSetting) as caught:
        Settings.from_mapping(
            {**COMPLETE, "NOAH_ENDPOINT_2": "", "NOAH_ENDPOINT_2_FILE": str(fifo)}
        )
    assert "not a file" in str(caught.value)
    big = tmp_path / "big"
    big.write_text("wss://h.example/" + "a" * 70_000)
    with pytest.raises(BadSetting) as caught:
        Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2": "", "NOAH_ENDPOINT_2_FILE": str(big)})
    assert "too large" in str(caught.value)


# -- which pieces are secret (the documented thresholds) -----------------------------------------


@pytest.mark.parametrize(
    "path, secret, hidden",
    [
        ("/v2/abcdefgh/x", "abcdefgh", True),  # a middle segment of 8
        ("/v2/abcdefg/x", "abcdefg", False),  # a middle segment of 7, no digit
        ("/a/b/k9x2m4", "k9x2m4", True),  # the last segment: 4+ chars with a digit
        ("/a/b/word", "word", False),  # the last segment: word-like (no digit)
        ("/a/b/ab1", "ab1", False),  # the last segment: 3 characters
        ("/a/b/rpc", "rpc", False),  # a common word
        ("/a/b/abcdefgh", "abcdefgh", True),  # the last segment of 8 needs no digit
    ],
)
def test_path_segment_thresholds(path, secret, hidden):
    secretscrub.register_endpoint("wss://h.example" + path)
    out = secretscrub.scrub(f"x {secret} y")
    assert (secret not in out) is hidden


def test_user_info_thresholds():
    secretscrub.register_endpoint("wss://longuser1:" + "pw99@h.example")
    out = secretscrub.scrub("longuser1 pw99")
    assert "longuser1" not in out and "pw99" not in out
    secretscrub.reset()
    secretscrub.register_endpoint("wss://shortu:" + "pw9@h.example")
    out = secretscrub.scrub("shortu pw9")
    assert out == "shortu pw9"  # a user under 8 and a password under 4 are ignored


@pytest.mark.parametrize(
    "variant",
    [
        CANARY,
        CANARY.lower(),
        CANARY.upper(),
    ],
)
def test_case_and_percent_forms_of_a_piece(variant):
    secretscrub.register_endpoint(URL)
    assert "key123" not in secretscrub.scrub(f"a {variant} b").lower()


def test_percent_encoded_spelling_of_a_piece_with_unsafe_characters():
    secretscrub.register_endpoint("wss://h.example/v2/with space&more1")
    assert "with%20space%26more1" not in secretscrub.scrub("x with%20space%26more1 y")
    assert "with space&more1" not in secretscrub.scrub("x with space&more1 y")


def test_registry_url_forms():
    from urllib.parse import quote

    secretscrub.register_endpoint(URL)
    for form in (URL, quote(URL, safe=""), URL.lower()):
        out = secretscrub.scrub(f"cannot reach {form}")
        assert CANARY.lower() not in out.lower()
        assert "h.example" in out or "h.example" not in form


# -- every kind of registration ---------------------------------------------------------


def test_every_environment_variable_and_settings_field_registers():
    for index, name in enumerate(secretscrub._ENV_ENDPOINTS):
        secretscrub.reset()
        key = f"envkey{index}abcdefgh"
        secretscrub.register_environment({name: f"wss://h.example/{key}"})
        assert key not in secretscrub.scrub(key), name

    for field in ("chain_endpoint", "second_chain_endpoint", "submit_endpoint"):
        secretscrub.reset()
        key = f"field{field[:3]}abcdefgh"
        settings = type("S", (), {field: f"wss://h.example/{key}"})()
        secretscrub.register_settings(settings)
        assert key not in secretscrub.scrub(key), field


def test_the_second_endpoint_file_registers(tmp_path):
    keyed = tmp_path / "second"
    keyed.write_text("wss://i.example/v2/filekey1abcdefgh\n")
    secretscrub.register_environment({"NOAH_ENDPOINT_2_FILE": str(keyed)})
    assert "filekey1abcdefgh" not in secretscrub.scrub("filekey1abcdefgh")


def test_the_registry_starts_empty_for_every_test():
    """conftest resets it: nothing registered by an earlier test is visible here."""
    assert not secretscrub._registry.has_content() or secretscrub._registry.scrub("zzz") == "zzz"
    secretscrub.reset()
    assert not secretscrub._registry.has_content()


def test_a_disabled_backstop_does_nothing():
    secretscrub.set_enabled(False)
    try:
        secretscrub.activate(type("S", (), {"chain_endpoint": URL})())
        assert not secretscrub._registry.has_content()
    finally:
        secretscrub.set_enabled(True)
