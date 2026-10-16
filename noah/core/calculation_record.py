"""Durable automatic calculation evidence, separate from publication and rewards.

The canonical score keeps the existing noah-score-output schema. Immutable,
content-addressed companions retain the exact twin-agreed bytes and inputs;
the publication intent links their manifest. No artifact here claims payment.
"""

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..scoring.fixture_io import render_output
from .settings import SettingsError


class CalculationEvidenceRefused(SettingsError):
    """The agreed calculation could not be retained; do not start a send."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return render_output(value).encode("utf-8")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path, data, *, replace=False):
    """Fsync bytes and directory; immutable writes never overwrite conflicts."""
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(exist_ok=True)
        _sync_directory(parent.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise CalculationEvidenceRefused("existing calculation evidence differs")
        _sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def source_identity():
    """Record a declared revision plus actual service source fingerprints.

    A Git/installation label alone is not a claim that installed bytes match it.
    Only public source files are hashed; no environment or wallet file is read.
    """
    root = Path(__file__).resolve().parents[2]
    revision = None
    marker = root / ".deployed-commit"
    if marker.is_file():
        for line in marker.read_text().splitlines():
            key, _, value = line.partition("=")
            if key == "commit" and re.fullmatch(r"[0-9a-f]{40}", value):
                revision = value
    if revision is None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}\n?", result.stdout):
                revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    files = {}
    for folder, pattern in (
        ("noah", "*.py"),
        ("neurons", "*.py"),
        ("verification/noah-ts/src", "*.ts"),
    ):
        for path in sorted((root / folder).rglob(pattern)):
            relative = path.relative_to(root)
            if "tests" not in relative.parts and "__pycache__" not in relative.parts:
                files[str(relative)] = digest(path.read_bytes())
    return {"declared_commit": revision, "source_sha256": digest(encoded(files)), "files": files}


def retain(directory, *, fixture, agreed_text, vector, context):
    """Commit all evidence before returning the immutable publication reference."""
    directory = Path(directory)
    score = json.loads(agreed_text)
    day = int(score["evalDay"])
    if day < 0 or str(fixture["evalDay"]) != str(day) or context["day"] != day:
        raise CalculationEvidenceRefused("calculation day differs from the pinned scoring day")
    refs = {}
    for name, data in (
        ("fixture", encoded(fixture)),
        ("agreed_score", agreed_text.encode("utf-8")),
        ("context", encoded(context)),
    ):
        sha = digest(data)
        relative = f"evidence/{sha}.json"
        durable_write(directory / relative, data)
        refs[name] = {"path": relative, "sha256": sha}
    manifest = {
        "schema": "noah-calculation-evidence/1",
        "day": day,
        "height": context["height"],
        "genesis_hash": context["genesis_hash"],
        "netuid": context["netuid"],
        "artifacts": refs,
        "agreement": "python_typescript_identical",
        "engine_weights_u16": score["weightsU16"],
        "publishable_weights_u16": {str(uid): int(weight) for uid, weight in vector.items()},
        "changed_uids": sorted(
            uid
            for uid in set(map(int, score["weightsU16"])) | set(vector)
            if int(score["weightsU16"].get(str(uid), 0)) != vector.get(uid, 0)
        ),
        "state": "calculated",
        "publication": "not_established",
        "settlement": "not_observed",
    }
    data = encoded(manifest)
    sha = digest(data)
    relative = f"evidence/{sha}.json"
    durable_write(directory / relative, data)
    # Preserve the existing score schema and canonical filename. Refuse a
    # conflicting earlier calculation rather than silently replacing history.
    canonical = {
        **score,
        "netuid": context["netuid"],
        "genesis_hash": context["genesis_hash"],
        "ark_address": context["ark_address"],
        "publishable_weights_u16": manifest["publishable_weights_u16"],
    }
    durable_write(directory / f"day-{day}.fixture.json", encoded(fixture))
    durable_write(directory / f"day-{day}.json", encoded(canonical))
    reference = {
        "path": relative,
        "sha256": sha,
        "day": day,
        "genesis_hash": context["genesis_hash"],
        "candidate_sha256": context.get("candidate", {}).get("source_sha256"),
    }
    durable_write(
        directory / f"day-{day}.calculation.json",
        encoded(
            {
                **manifest,
                "reference": reference,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        replace=True,
    )
    return reference


def verify(directory, reference):
    """Verify all linked bytes during recovery; missing evidence is a refusal."""

    def read(ref):
        sha = ref.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or ref.get("path") != f"evidence/{sha}.json":
            raise CalculationEvidenceRefused("invalid calculation artifact reference")
        data = (Path(directory) / ref["path"]).read_bytes()
        if digest(data) != sha:
            raise CalculationEvidenceRefused("calculation artifact hash differs")
        return json.loads(data)

    manifest = read(reference)
    for ref in manifest["artifacts"].values():
        read(ref)
    return manifest
