"""The backstop: once an endpoint is known, its key cannot reach output, a traceback or a log."""

import io
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from noah import secretscrub
from noah.chain import sources

pytestmark = pytest.mark.usefixtures("backstop")

ROOT = Path(__file__).resolve().parents[3]
CANARY = "CANARY" + "KEY123"
URL = f"wss://h.example/v2/{CANARY}"


@pytest.fixture(autouse=True)
def clean_registry():
    secretscrub.reset()
    yield
    secretscrub.reset()


def test_the_whole_url_becomes_its_label():
    secretscrub.register_endpoint(URL)
    assert secretscrub.scrub(f"cannot reach {URL} now") == "cannot reach wss://h.example now"


@pytest.mark.parametrize(
    "variant",
    [CANARY, CANARY.lower(), CANARY.upper(), quote(CANARY), f"/{CANARY}", f"x{CANARY}y"],
)
def test_a_long_path_segment_is_hidden_in_any_spelling(variant):
    secretscrub.register_endpoint(URL)
    assert CANARY.lower() not in secretscrub.scrub(f"a {variant} b").lower()


def test_percent_encoded_pieces():
    keyed = "wss://h.example/with space and more"
    secretscrub.register_endpoint(keyed)
    assert "space and" not in secretscrub.scrub("with%20space%20and%20more happened")


def test_user_info_and_query_values():
    query = "&".join(["sig=" + "abcd", "other=short", "t" + "oken=longvalue123"])
    secretscrub.register_endpoint("wss://someone:" + "pw12@h.example/p?" + query)
    out = secretscrub.scrub("pw12 abcd short longvalue123 someone")
    assert "pw12" not in out and "abcd" not in out and "longvalue123" not in out
    assert "short" in out  # a short value under a name that is not key-like is ignored


def test_short_and_common_pieces_are_ignored_so_ordinary_words_survive():
    secretscrub.register_endpoint("wss://h.example/rpc/v2/ws?x=1")
    text = "the rpc ws v2 node answered 1"
    assert secretscrub.scrub(text) == text


def test_a_short_last_segment_is_a_key_position():
    secretscrub.register_endpoint("wss://h.example/rpc/k9x2m4")
    assert "k9x2m4" not in secretscrub.scrub("bad k9x2m4 here")
    assert "rpc" in secretscrub.scrub("the rpc path")


def test_the_registry_never_shows_its_contents():
    secretscrub.register_endpoint(URL)
    assert CANARY not in repr(secretscrub._registry)


def test_a_registered_settings_object():
    class S:
        chain_endpoint = URL
        second_chain_endpoint = None
        submit_endpoint = "wss://s.example/" + CANARY + "-submit"

    secretscrub.register_settings(S())
    out = secretscrub.scrub(f"{CANARY}-submit and {CANARY}")
    assert CANARY not in out


def test_the_environment_and_the_endpoint_file_are_registered(tmp_path):
    keyed = tmp_path / "endpoint-2"
    keyed.write_text("wss://i.example/FILEKEYABCDEF\n")
    secretscrub.register_environment(
        {"NOAH_CHAIN_ENDPOINT": URL, "NOAH_ENDPOINT_2_FILE": str(keyed), "NOAH_AUTHOR_WS": ""}
    )
    out = secretscrub.scrub(f"{CANARY} FILEKEYABCDEF")
    assert CANARY not in out and "FILEKEYABCDEF" not in out


def test_the_stream_wrapper_scrubs_and_passes_everything_else_through():
    secretscrub.register_endpoint(URL)
    sink = io.StringIO()
    stream = secretscrub.ScrubbingStream(sink)
    stream.write(f"key is {CANARY}\n")
    stream.writelines([f"again {CANARY}\n"])
    assert CANARY not in sink.getvalue()
    assert stream.getvalue() == sink.getvalue()  # attribute access passes through


def test_the_log_record_factory_uses_the_same_pieces():
    secretscrub.register_endpoint(URL)
    sources.guard_endpoint_logging()
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Collect()
    logger = logging.getLogger("websockets.client")
    logger.addHandler(handler)
    try:
        logger.warning("rejected request to /%s", CANARY)  # only the key, not the whole URL
    finally:
        logger.removeHandler(handler)
    assert seen and CANARY not in seen[0]


THROWAWAY = """
import logging, os, sys
sys.path.insert(0, {root!r})
from noah.chain.sources import guard_endpoint_logging
guard_endpoint_logging()
key = os.environ["KEY"]
print("stdout has", key)
print("stderr has", key, file=sys.stderr)
sys.stdout.write("raw write " + key.lower() + "\\n")
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("websockets").warning("path /%s", key)
raise RuntimeError("uncaught, mentions " + key)
"""


def test_a_throwaway_script_that_prints_the_key_at_an_unlisted_site(tmp_path):
    script = tmp_path / "throwaway.py"
    script.write_text(THROWAWAY.format(root=str(ROOT)))
    keyed = tmp_path / "second"
    keyed.write_text("wss://i.example/v2/" + CANARY + "\n")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "KEY": CANARY,
        "NOAH_ENDPOINT_2_FILE": str(keyed),
    }
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "stdout has" in combined and "uncaught" in combined  # it did run and did fail
    assert CANARY.lower() not in combined


def test_settings_driven_activation_in_a_subprocess(tmp_path):
    script = tmp_path / "settings_run.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from noah import secretscrub\n"
        "class S:\n"
        f"    chain_endpoint = {URL!r}\n"
        "    second_chain_endpoint = None\n"
        "    submit_endpoint = None\n"
        "secretscrub.activate(S())\n"
        f"print('leak', {CANARY!r}, {quote(CANARY)!r})\n"
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=60,
    )
    assert result.returncode == 0 and "leak" in result.stdout
    assert CANARY not in result.stdout


def test_install_routes_the_exception_hook_and_the_streams():
    secretscrub.install()
    assert sys.excepthook is secretscrub._excepthook
    assert isinstance(sys.stdout, secretscrub.ScrubbingStream)
    assert isinstance(sys.stderr, secretscrub.ScrubbingStream)
    secretscrub.register_endpoint(URL)
    sink = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = secretscrub.ScrubbingStream(sink)
    try:
        try:
            raise RuntimeError(f"bad {CANARY}")
        except RuntimeError:
            secretscrub._excepthook(*sys.exc_info())
    finally:
        sys.stderr = real_stderr
    assert "RuntimeError" in sink.getvalue() and CANARY not in sink.getvalue()
