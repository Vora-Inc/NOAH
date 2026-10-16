"""Bounded diagnostic files; never used as calculation or chain evidence."""

import fcntl
import json
import os
import stat
import uuid
from contextlib import contextmanager


@contextmanager
def locked_directory(path):
    """Use a stable lock across rotation and refuse symlink path components."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open("/", flags)
    lock = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        lock = os.open(
            path.name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
            raise ValueError("invalid diagnostic lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _regular(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("diagnostic destination must be a regular unlinked file")
    return True


def append(path, data, *, max_bytes, backups):
    with locked_directory(path) as directory:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
        fd = os.open(path.name, flags, 0o600, dir_fd=directory)
        rotated = False
        try:
            _regular(directory, path.name)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                raise ValueError("incomplete diagnostic tail")
            if len(data) > max_bytes:
                raise ValueError("diagnostic record exceeds file limit")
            if size + len(data) > max_bytes:
                # Check the entire bounded set before changing any member.
                present = {
                    i: _regular(directory, f"{path.name}.{i}") for i in range(1, backups + 1)
                }
                for i in range(backups - 1, 0, -1):
                    if present[i]:
                        os.rename(
                            f"{path.name}.{i}",
                            f"{path.name}.{i + 1}",
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                        )
                os.rename(path.name, path.name + ".1", src_dir_fd=directory, dst_dir_fd=directory)
                os.close(fd)
                fd = None
                fd = os.open(path.name, flags | os.O_EXCL, 0o600, dir_fd=directory)
                os.fsync(directory)
                size = 0
                rotated = True
            if os.write(fd, data) != len(data):
                os.ftruncate(fd, size)
                raise OSError("short diagnostic write")
            os.fsync(fd)
            space = os.fstatvfs(directory)
            return {
                "active_bytes": size + len(data),
                "rotated": rotated,
                "free_bytes": str(space.f_bavail * space.f_frsize),
            }
        finally:
            if fd is not None:
                os.close(fd)


def write_health(path, health):
    """Atomic latest-writer health snapshot with no raw errors or endpoint text."""
    with locked_directory(path) as directory:
        name = path.name + ".health.json"
        _regular(directory, name)
        space = os.fstatvfs(directory)
        free_bytes = str(space.f_bavail * space.f_frsize)
        health = {**health, "free_bytes_decimal": free_bytes}
        temp = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = None
        try:
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            data = (json.dumps(health, sort_keys=True) + "\n").encode()
            if os.write(fd, data) != len(data):
                raise OSError("short health write")
            os.fsync(fd)
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            return free_bytes
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
