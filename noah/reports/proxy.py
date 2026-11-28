"""Report-only proxy routing and one durable sender lane shared by a local fleet.

All users of one delegate must use the same state_dir on one host. The miner
hotkey is only the effective origin; it is never loaded by this module.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .backing.keys import ss58_decode
from .common import ReportError, immutable_json, locked, read_json


def route(config):
    delegate = getattr(config, "report_proxy_address", None)
    if not delegate:
        return None
    try:
        if ss58_decode(delegate) == ss58_decode(config.miner_hotkey):
            raise ValueError("same account")
    except (ValueError, TypeError):
        raise ReportError("report proxy must be a valid address different from the miner") from None
    return {"real": config.miner_hotkey, "delegate": delegate, "netuid": config.netuid}


def _lane(config):
    return (
        Path(config.state_dir).resolve()
        / "reports"
        / config.genesis_hash.removeprefix("0x")
        / "proxy-senders"
        / ss58_decode(route(config)["delegate"]).hex()
    )


def _write(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".sender-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _discard(path):
    path.unlink()
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _journal(config, marker):
    if (
        not isinstance(marker, dict)
        or set(marker) != {"directory", "phase"}
        or marker["phase"] not in ("commit", "reveal")
    ):
        raise ReportError("invalid proxy sender record; preserve it for recovery")
    directory = Path(marker["directory"]).resolve()
    if not directory.is_relative_to(Path(config.state_dir).resolve() / "reports"):
        raise ReportError("proxy sender record lies outside report state")
    path = directory / (marker["phase"] + ".journal")
    if path.exists() and not path.is_file():
        raise ReportError("invalid proxy sender record; preserve it for recovery")
    return path


def _terminal(config, marker):
    """True when the phase journal is finished.

    None means the marker has no phase journal. Nothing was signed or
    broadcast: the marker is not an in-flight transaction.
    """
    path = _journal(config, marker)
    if not path.exists():
        return None
    return read_json(path).get("state") in ("finalized", "failed")


@contextmanager
def sender_lane(config, directory):
    if route(config) is None:
        yield
        return
    lane = _lane(config)
    with locked(lane):
        active = lane / "active.json"
        if active.exists():
            marker = read_json(active)
            finished = _terminal(config, marker)
            if finished is None:
                _discard(active)
            elif not finished and Path(marker["directory"]).resolve() != directory.resolve():
                raise ReportError(
                    "unfinished proxy submission for another miner/round; "
                    "resume its saved journals first"
                )
        yield


def bind_route(config, directory, existing):
    desired = route(config)
    path = directory / "signing-route.json"
    if desired is None:
        if path.exists() and read_json(path) != {"mode": "direct"}:
            raise ReportError(
                "report signing route changed; restore the original config to recover"
            )
        return
    if (existing or any(directory.glob("*.journal"))) and not path.exists():
        raise ReportError(
            "existing report has no proxy route; recover it using its original direct config"
        )
    immutable_json(path, {"mode": "proxy", **desired})


def claim(config, directory, phase):
    if route(config) is not None:
        _write(
            _lane(config) / "active.json", {"directory": str(directory.resolve()), "phase": phase}
        )
