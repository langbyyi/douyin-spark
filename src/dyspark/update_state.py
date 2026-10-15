"""Standard-library update protocol shared by the host and application processes."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from dyspark.locking import LockBusyError, file_lock

SHA = re.compile(r"[0-9a-f]{40}\Z")
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
ACTIVE_PHASES = frozenset({"queued", "preparing", "switching", "verifying", "recovering"})
_held = threading.local()


class UpdateError(ValueError):
    """An update cannot safely proceed."""


def state_dir(data: Path) -> Path:
    folder = data / "updater"
    if folder.is_symlink():
        raise UpdateError("更新目录不能是符号链接")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Docker's non-root app and a root host manager must share the data owner.
    if os.name == "posix" and os.geteuid() == 0:
        owner = data.stat()
        os.chown(folder, owner.st_uid, owner.st_gid)
    return folder


def read_state(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise UpdateError(f"更新状态文件异常：{path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"无法读取更新状态：{path.name}") from exc


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise UpdateError(f"更新状态文件不能是符号链接：{path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            if os.name == "posix" and os.geteuid() == 0:
                owner = path.parent.stat()
                os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_available(data: Path) -> None:
    path = state_dir(data) / "maintenance.json"
    if path.exists() or path.is_symlink():
        raise UpdateError("程序正在更新，暂时不能启动任务或修改设置")


@contextlib.contextmanager
def operation(data: Path, *, timeout: float = 0) -> Iterator[None]:
    key = str(data.resolve())
    roots = getattr(_held, "roots", set())
    if key in roots:
        ensure_available(data)
        yield
        return
    try:
        with file_lock(state_dir(data) / "operation.lock", timeout=timeout):
            ensure_available(data)
            _held.roots = roots | {key}
            try:
                yield
            finally:
                _held.roots = roots
    except LockBusyError as exc:
        raise UpdateError("更新或其他操作正在使用数据目录，请稍后重试") from exc


@contextlib.contextmanager
def activity(data: Path) -> Iterator[None]:
    """Reserve a CLI lifetime without holding the short-lived mutation gate."""
    try:
        with contextlib.ExitStack() as stack:
            # The launcher publishes metadata while holding this gate. A new child
            # must wait for its own parent, then recheck the maintenance boundary.
            with operation(data, timeout=10):
                stack.enter_context(file_lock(state_dir(data) / "activity.lock"))
            yield
    except LockBusyError as exc:
        raise UpdateError("已有任务在运行，暂时不能更新或启动其他任务") from exc


def begin_maintenance(data: Path, request: dict[str, Any], idle: Callable[[], None]) -> None:
    try:
        with operation(data), file_lock(state_dir(data) / "activity.lock"):
            idle()
            folder = state_dir(data)
            write_state(folder / "request.json", request)
            write_state(folder / "maintenance.json", request)
    except LockBusyError as exc:
        raise UpdateError("已有任务在运行，请等待结束后更新") from exc


def end_maintenance(data: Path, request_id: str) -> None:
    path = state_dir(data) / "maintenance.json"
    state = read_state(path)
    if state and state.get("id") != request_id:
        raise UpdateError("更新请求不匹配，保留维护状态以便核对")
    path.unlink(missing_ok=True)
