"""Bounded raw transcript chunks for package/3; all bytes remain replay inputs."""
import gzip
import json
import os
from pathlib import Path

from .adjudication_contract import require

CHUNK_BYTES = 32 * 1024**2
MAX_CHUNKS = 32
KINDS = ('responses', 'engine')


def paths(root, kind):
    require(kind in KINDS, 'unknown shared stream')
    found = sorted(Path(root).glob(kind + '-*.jsonl.gz'))
    require(1 <= len(found) <= MAX_CHUNKS and all(
        path.name == f'{kind}-{index:04d}.jsonl.gz' for index, path in enumerate(found)),
        'shared stream chunks are missing or noncontiguous')
    return found


def write(root, kind, rows):
    require(kind in KINDS, 'unknown shared stream')
    chunk = None; handle = None; count = size = 0
    try:
        for row in rows:
            raw = (json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n').encode()
            require(0 < len(raw) <= 8 * 1024**2, 'shared stream line exceeds bound')
            if chunk is None or size + len(raw) > CHUNK_BYTES:
                if chunk is not None:
                    chunk.close(); handle.close()
                require(count < MAX_CHUNKS, 'shared stream chunk count exceeds bound')
                handle = os.fdopen(os.open(Path(root) / f'{kind}-{count:04d}.jsonl.gz',
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
                chunk = gzip.GzipFile(fileobj=handle, mode='wb', mtime=0)
                count += 1; size = 0
            chunk.write(raw); size += len(raw)
        require(count > 0, 'shared stream cannot be empty')
    finally:
        if chunk is not None:chunk.close()
        if handle is not None:handle.close()


def load(root, kind):
    rows = []
    for path in paths(root, kind):
        expanded = 0
        with gzip.open(path, 'rb') as stream:
            while True:
                raw = stream.readline(8 * 1024**2 + 1)
                if not raw:break
                expanded += len(raw)
                require(raw.endswith(b'\n') and len(raw) <= 8 * 1024**2 and expanded <= CHUNK_BYTES,
                        'shared stream expansion or line bound')
                rows.append(json.loads(raw))
    return rows
