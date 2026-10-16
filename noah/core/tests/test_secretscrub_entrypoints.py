"""The backstop as production wires it, in a real process: the endpoint is known only the way the
entry point learns it (a settings FILE for the validator), and a print of the key from a place no
fix covers is hidden. Removing the registration call from an entry point fails here."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from .test_settings import COMPLETE

ROOT = Path(__file__).resolve().parents[3]
CANARY = "CANARY" + "KEY123"
KEYED = f"wss://first.example/v2/{CANARY}"
PROFILE = next(
    parent / "docs" / "parameters" / "profiles" / "m1-revised-v1.json"
    for parent in Path(__file__).resolve().parents if (
        parent / "docs" / "parameters" / "profiles" / "m1-revised-v1.json"
    ).is_file()
)


def run(code, env_extra=None, args=()):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NOAH_")  # nothing endpoint-like comes from the environment
    }
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)})
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )


def settings_file(tmp_path, **override):
    mapping = {
        **COMPLETE,
        "NOAH_NETWORK": "local",
        "NOAH_LOCAL_GENESIS": "0x" + "ab" * 32,
        "NOAH_CHAIN_ENDPOINT": KEYED,
        "NOAH_PARAMETERS_JSON": str(PROFILE),
        **override,
    }
    path = tmp_path / "validator.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in mapping.items() if v != ""))
    return path


def combined(result):
    return (result.stdout + result.stderr).lower()


HOOK = """
import sys
sys.path.insert(0, {root!r})
import neurons.validator as v
from noah.core.settings import SettingsError
KEY = {key!r}

def leak(*args, **kwargs):
    print("unlisted site, stdout:", KEY)
    print("unlisted site, stderr:", KEY.lower(), file=sys.stderr)
    raise SettingsError("stop here")

v.connect = leak
sys.exit(v.main(["--env", {envfile!r}, "--dry-run"]))
"""


def test_the_validator_registers_an_endpoint_that_only_its_env_file_holds(tmp_path):
    envfile = settings_file(tmp_path)
    result = run(HOOK.format(root=str(ROOT), key=CANARY, envfile=str(envfile)))
    text = combined(result)
    assert "unlisted site, stdout:" in text and "unlisted site, stderr:" in text, text[-400:]
    assert CANARY.lower() not in text


def test_the_second_endpoint_file_is_registered_too(tmp_path):
    second = tmp_path / "second"
    second.write_text(f"wss://second.example/v2/{CANARY}-two\n")
    envfile = settings_file(
        tmp_path, NOAH_CHAIN_ENDPOINT="wss://first.example", NOAH_ENDPOINT_2_FILE=str(second)
    )
    result = run(HOOK.format(root=str(ROOT), key=CANARY + "-two", envfile=str(envfile)))
    assert "unlisted site" in combined(result)
    assert CANARY.lower() not in combined(result)


SCRIPT_HOOK = """
import importlib.util, sys
sys.path.insert(0, {root!r})
import neurons.validator as v
from noah.core.settings import SettingsError
KEY = {key!r}

def leak(*args, **kwargs):
    print("unlisted site:", KEY)
    raise SettingsError("stop here")

v.connect = leak
spec = importlib.util.spec_from_file_location("entry", {script!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.exit(module.main({argv!r}) or 0)
"""


@pytest.mark.parametrize(
    "script, argv",
    [
        ("day_close_pipeline.py", ["--height", "1", "--out-dir", "OUT"]),
    ],
)
def test_the_scripts_that_build_settings_register_the_endpoint(tmp_path, script, argv):
    mapping = {**COMPLETE, "NOAH_NETWORK": "local", "NOAH_LOCAL_GENESIS": "0x" + "ab" * 32}
    mapping.update({"NOAH_CHAIN_ENDPOINT": KEYED, "NOAH_PARAMETERS_JSON": str(PROFILE)})
    env = {k: v for k, v in mapping.items() if v != ""}
    argv = [str(tmp_path) if item == "OUT" else item for item in argv]
    code = SCRIPT_HOOK.format(
        root=str(ROOT), key=CANARY, script=str(ROOT / "scripts" / script), argv=argv
    )
    result = run(code, env_extra=env)
    text = combined(result)
    assert "unlisted site:" in text, text[-500:]
    assert CANARY.lower() not in text
