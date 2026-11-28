"""Verify optional, operator-installed capability approvals for exact chain runtimes.

The configured public key is a local trust root. The file is untrusted until its
signature and every record validate. A missing file leaves bundled approvals
available. New approvals are loaded from disk at the point of use.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .sources import Unverified

SCHEMA = b"noah-runtime-approvals/1"
APPROVALS_ENV = "NOAH_RUNTIME_APPROVALS"
KEY_ENV = "NOAH_RUNTIME_APPROVAL_KEY"
MAX_BYTES = 256 * 1024
CAPABILITIES = frozenset({"burn", "ema", "reports", "token", "weights"})
HASH = re.compile(r"0x[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


class RuntimeApprovalRequired(Unverified):
    """An exact runtime needs review before a chain operation can proceed."""

    def __init__(self, capability, *, wait_for_registry=True):
        self.capability = capability
        self.wait_for_registry = wait_for_registry
        detail = "code and semantics have not been reviewed" if wait_for_registry else "changed during preparation"
        super().__init__(f"{capability} runtime {detail}")


@dataclass(frozen=True)
class Approval:
    genesis: str
    spec: int
    transaction: int
    code: str
    metadata: str
    source: str
    capabilities: frozenset[str]


def parse(payload):
    """Parse canonical ASCII rows. Field order and newline bytes are signed."""
    lines = payload.decode("ascii").split("\n")
    if lines[0] != SCHEMA.decode() or lines[-1] != "" or not 2 < len(lines) <= 1026:
        raise ValueError("invalid runtime approvals format")
    approvals = {}
    previous = None
    for row in lines[1:-1]:
        genesis, spec, transaction, code, metadata, source, raw_capabilities = row.split("\t")
        capabilities = raw_capabilities.split(",")
        if not (HASH.fullmatch(genesis) and HASH.fullmatch(code) and SHA256.fullmatch(metadata)
                and COMMIT.fullmatch(source) and all(re.fullmatch(r"(0|[1-9][0-9]{0,9})", value)
                                                     for value in (spec, transaction))
                and capabilities == sorted(set(capabilities)) and set(capabilities) <= CAPABILITIES
                and ("ema" not in capabilities or "token" in capabilities)):
            raise ValueError("invalid runtime approval")
        spec, transaction = int(spec), int(transaction)
        identity = (genesis, spec)
        if max(spec, transaction) >= 2**32 or previous is not None and identity <= previous:
            raise ValueError("unordered or duplicate runtime approval")
        previous = identity
        approvals[identity] = Approval(genesis, spec, transaction, code, metadata, source,
                                       frozenset(capabilities))
    return approvals


def verify(data, key):
    """Check an Ed25519 signature over the exact payload, including its domain."""
    if len(data) > MAX_BYTES or not SHA256.fullmatch(key):
        raise ValueError("invalid runtime approval key or size")
    signature, sep, payload = data.partition(b"\n")
    if not sep or not re.fullmatch(rb"[0-9a-f]{128}", signature):
        raise ValueError("invalid runtime approval signature encoding")
    from Crypto.Signature import eddsa
    public = eddsa.import_public_key(bytes.fromhex(key))
    eddsa.new(public, "rfc8032").verify(payload, bytes.fromhex(signature.decode("ascii")))
    return parse(payload)


def read(path, key):
    with Path(path).open("rb") as stream:
        return verify(stream.read(MAX_BYTES + 1), key)


def configured():
    return bool(os.environ.get(APPROVALS_ENV) and os.environ.get(KEY_ENV))


def approved_runtime(genesis, spec, capability):
    path = os.environ.get(APPROVALS_ENV)
    key = os.environ.get(KEY_ENV)
    if not path or not key or type(spec) is not int:
        return None
    try:
        row = read(path, key).get((genesis, spec))
    except (OSError, ImportError, ValueError):
        return None
    return row if row is not None and capability in row.capabilities else None


def lookup(genesis, spec, transaction, code, capability):
    """Require all identity fields and an explicitly reviewed capability."""
    if type(transaction) is not int:
        return None
    row = approved_runtime(genesis, spec, capability)
    return row if row is not None and (row.transaction, row.code) == (transaction, code) else None


def installed_revision():
    """Cheap change hint only. Callers re-verify contents before using approvals."""
    path = os.environ.get(APPROVALS_ENV)
    if path:
        try:
            stat = Path(path).stat()
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            pass
    return None
