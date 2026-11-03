"""Verify starter allocation with an independently implemented integer allocator."""

import json
import subprocess
from pathlib import Path

from ..core.publish_intent import PublicationStatusUnknown


def wire(value, name=""):
    if isinstance(value, dict):
        return {k: wire(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [wire(v, name) for v in value]
    if name.endswith("Rao") and type(value) is int:
        return str(value)
    return value


def check(raw, previous, result):
    try:
        proc = subprocess.run(
            ["node", str(Path(__file__).with_name("verify-allocation.mjs"))],
            input=json.dumps(wire(dict(input=raw, previous=previous, result=result))),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if proc.returncode or proc.stdout != "verified\n":
            raise ValueError("starter allocation implementations disagree")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PublicationStatusUnknown(
            "independent starter verification unavailable or disagrees"
        ) from exc
