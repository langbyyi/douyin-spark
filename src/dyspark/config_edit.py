"""Validated, serialized edits to config.toml that preserve comments."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import tomlkit

from dyspark.config import Settings, SettingsError, read_config_text, settings_from_text
from dyspark.locking import LockBusyError, file_lock

_EDIT_LOCK = threading.Lock()


class ConfigEditError(ValueError):
    """An edit failed; validation failures leave the original file unchanged."""


def edit_config(path: Path, change: Callable[[tomlkit.TOMLDocument], None]) -> Settings:
    with _EDIT_LOCK:
        try:
            with file_lock(path.with_name(f".{path.name}.edit.lock"), timeout=5):
                return _edit_locked(path, change)
        except LockBusyError as exc:
            raise ConfigEditError("另一个进程正在编辑配置，请稍后重试") from exc
        except OSError as exc:
            raise ConfigEditError(f"无法锁定配置文件，请检查目录权限：{exc}") from exc


def _edit_locked(path: Path, change: Callable[[tomlkit.TOMLDocument], None]) -> Settings:
    try:
        text = read_config_text(path)
        doc = tomlkit.parse(text)
    except SettingsError as exc:
        raise ConfigEditError(str(exc)) from exc
    except Exception as exc:
        raise ConfigEditError(f"{path} 语法错误：{exc}") from exc
    change(doc)
    new_text = tomlkit.dumps(doc)
    try:
        settings = settings_from_text(new_text, path)
    except SettingsError as exc:
        raise ConfigEditError(f"修改后的配置无效，未保存：{exc}") from exc
    write_config(path, new_text)
    return settings


def write_config(path: Path, text: str) -> None:
    """Replace atomically; only mount-point EBUSY permits an in-place fallback."""
    data = text.encode("utf-8")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            shutil.copymode(path, tmp)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            # Linux single-file bind mounts cannot be replaced. Keep their
            # existing inode; ordinary disk/permission errors never use this path.
            with path.open("r+b") as handle:
                handle.write(data)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise ConfigEditError(
            f"无法写入 {path}（{exc.strerror or exc}）。请检查磁盘空间、目录及文件写入权限；"
            "Docker 中请确认配置可写，并授权容器用户（uid 1000）"
        ) from exc
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
