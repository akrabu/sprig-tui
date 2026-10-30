"""Durable replacement and cooperative per-file locking on POSIX systems."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile


MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


class FileBusyError(OSError):
    pass


class CommittedWriteError(OSError):
    """The rename succeeded, but directory durability could not be confirmed."""


def digest(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def read_limited(path: Path, limit: int = MAX_DOCUMENT_BYTES) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"File exceeds the {limit // 1024 // 1024} MiB limit: {path.name}")
    return data


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


@contextmanager
def file_lock(path: Path):
    path = path.expanduser().resolve()
    directory = path.parent / ".sprig-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(str(path.absolute()).encode()).hexdigest()
    fd = os.open(directory / (key + ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FileBusyError(f"Another Sprig process is writing {path.name}; retry after it finishes.") from error
        yield
    finally:
        os.close(fd)


def atomic_write(path: Path, content: bytes, mode: int = 0o600, before_replace=None) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        try:
            sync_directory(path.parent)
        except OSError as error:
            raise CommittedWriteError(f"{path.name} was written, but directory sync failed: {error}") from error
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
