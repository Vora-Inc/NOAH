"""A static error outside exercised paths must make the combined runner fail."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "run-tests.sh"


def shell_stub(path, content):
    path.write_text("#!/usr/bin/env bash\n" + content + "\n")
    path.chmod(0o755)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("answer = 42\n", "PASS"),
        ("answer = undefined_name\n", "FAIL"),
        ("def broken(:\n", "FAIL"),
    ],
)
def test_static_gate_controls_runner_result(tmp_path, source, expected):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(RUNNER, scripts / "run-tests.sh")
    # Isolate the static gate; all other stages explicitly succeed in this harness.
    shell_stub(
        scripts / "run-tests-python.sh",
        "\n".join(
            [
                "echo 'RESULT validator STATE=PASS NOTE=\"1 passed\"'",
                "echo 'RESULT miner-tool STATE=PASS NOTE=\"1 passed\"'",
                "echo 'RESULT combined STATE=PASS NOTE=\"2 passed, 0 failed\"'",
                f"echo 'RESOLVED miner-tool={sys.executable}'",
            ]
        ),
    )
    shell_stub(scripts / "twin-proof.sh", "echo '1 of 1 fixtures byte-identical'")
    (scripts / "check_examples.py").write_text("print('PASS: fixture harness')\n")
    (tmp_path / "unexercised.py").write_text(source)
    (tmp_path / "verification/noah-ts/node_modules/typescript").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    shell_stub(tools / "npm", "echo 'pass 1'")
    shell_stub(tools / "npx", "exit 0")
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        ["bash", str(scripts / "run-tests.sh")], env=env, capture_output=True, text=True, timeout=20
    )
    assert f"{expected}  Python static errors" in result.stdout, result.stdout + result.stderr
    assert result.returncode == (0 if expected == "PASS" else 1)
