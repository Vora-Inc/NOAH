"""Access points state relative to the configured scores directory."""

import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def directory(root, *, create=False):
    """Pin an operator-configured directory without following path symlinks."""
    path = Path(root).absolute()
    if ".." in path.parts:
        raise ValueError("unsafe scores directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _check_name(name):
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", name):
        raise ValueError("unsafe scores file name")


def read(fd, name):
    """Return bytes for an ordinary file and None for an absent entry."""
    _check_name(name)
    try:
        entry = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(entry).st_mode):
            raise ValueError("scores state must be a regular file")
        with os.fdopen(entry, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(entry)


def read_file(root, name, *, required=False):
    try:
        with directory(root) as fd:
            data = read(fd, name)
    except FileNotFoundError:
        data = None
    if required and data is None:
        raise FileNotFoundError(name)
    return data


def write(fd, name, data, *, replace=False):
    """Fsync policy bytes and preserve immutable predecessors across retries."""
    _check_name(name)
    temp = f".{name}.{uuid.uuid4().hex}.tmp"
    entry = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=fd)
    try:
        with os.fdopen(entry, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            try:
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                if read(fd, name) != data:
                    raise ValueError("existing policy state differs")
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
