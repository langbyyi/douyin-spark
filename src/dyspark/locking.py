"""Persistent OS file locks shared by independent threads and processes."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusyError(RuntimeError):
    """Another process or thread holds the requested lock."""


@contextmanager
def file_lock(path: Path, timeout: float = 0) -> Iterator[None]:
    """Lock a persistent file; crashes release the lock, never unlink the file."""
    if timeout < 0:
        raise ValueError("锁等待时间不能为负数")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            os.fchmod(handle.fileno(), 0o600)

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusyError(f"文件正在使用：{path.name}") from None
                time.sleep(min(0.01, remaining))
        try:
            # Windows permits locking beyond EOF; initialize only AFTER acquiring.
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            yield
        finally:
            release()
