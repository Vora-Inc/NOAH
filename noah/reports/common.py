"""Round identity and durable local state shared by commands and schedulers."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path


class ReportError(ValueError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_round(path):
    from noah.reports.verification import validate_round
    contract = read_json(path)
    validate_round(contract)
    return contract


def immutable_json(path, value):
    """Publish a complete file atomically; identical retries are harmless."""
    from noah.reports.backing.prepare import _publish_exclusive
    path = Path(path)
    if path.exists():
        if read_json(path) != value:
            raise ReportError("existing report state differs; preserve it and use the original round")
        return
    _publish_exclusive(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReportError("another report operation owns this directory") from None
        yield
    finally:
        os.close(fd)


def bound_config(config, contract):
    from noah.reports.backing.keys import ss58_decode
    from noah.reports.verification import validate_round
    validate_round(contract)
    if not config.genesis_hash or config.genesis_hash.removeprefix("0x").lower() != contract["genesis_hash"]:
        raise ReportError("miner configuration and trusted round must name the same genesis")
    if config.netuid != contract["mining_netuid"]:
        raise ReportError("miner configuration and round mining subnet differ")
    for address, field in ((config.ark_coldkey, "ark_coldkey"), (config.validator_hotkey, "ark_validator_hotkey")):
        if ss58_decode(address).hex() != contract[field]:
            raise ReportError("miner configuration and round Ark destination differ")


def state_directory(config, contract):
    from noah.reports.backing.keys import ss58_decode
    bound_config(config, contract)
    return (Path(config.state_dir) / "reports" / contract["genesis_hash"]
            / str(config.netuid) / ss58_decode(config.miner_hotkey).hex() / digest(contract))
