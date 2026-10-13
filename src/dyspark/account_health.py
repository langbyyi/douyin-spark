"""Durable record of what the last live probe saw for one account.

User intent lives only in ``config.toml`` (``enabled``). This file answers two
questions and nothing else: what did the last probe see (``status`` + ``reason``
+ the IP/proxy fields), and does that verdict need the user to act before sending
may resume?

``paused`` is derived from ``status`` rather than stored, so the two can never
disagree. Only verdicts the program cannot clear by itself stop sending; transient
ones are recorded and retried by the next run, which re-checks the account live
before every send anyway.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dyspark.locking import LockBusyError, file_lock
from dyspark.store import DataDir, RunBusyError, write_json_atomic

# 需要用户处理（重新扫码、处理安全验证、换回原出口）才能恢复的结论：停发。
NEEDS_USER = frozenset({"logged_out", "blocked", "ip_mismatch"})
# 程序下次运行现场重新核对即可的结论：记录原因，但不停发。
TRANSIENT = frozenset({"network_error", "corrupt"})
STATUSES = NEEDS_USER | TRANSIENT | {"unknown", "ready"}
# 旧版本用 "paused" 记录「用户停用」；用户意愿现在只看 config 的 enabled，读到即归为 unknown。
_LEGACY = frozenset({"paused"})
FIELDS = frozenset({"checked_at", "current_ip", "login_ip", "proxy_fingerprint"})
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def stops_sending(status: str) -> bool:
    """True when this verdict must stop sending until the user acts on it."""
    return status in NEEDS_USER


class HealthStore:
    def __init__(self, data_dir: Path) -> None:
        self.data = DataDir(data_dir)

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or name.upper() in _RESERVED:
            raise ValueError("非法账号名")
        return self.data.account_dir(name) / "health.json"

    @contextmanager
    def _locked(self, path: Path):
        try:
            with file_lock(path.with_name(".health.lock"), timeout=5):
                yield
        except LockBusyError:
            raise RunBusyError("账号健康状态正在更新，请重试") from None

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "status": "unknown",
            "reason": "",
            "checked_at": None,
            "current_ip": None,
            "login_ip": None,
            "proxy_fingerprint": None,
        }

    @staticmethod
    def _view(value: dict[str, Any]) -> dict[str, Any]:
        """Add the derived ``paused`` flag; callers never store it."""
        return value | {"paused": stops_sending(value["status"])}

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            valid = (
                isinstance(value, dict)
                and value.get("status") in STATUSES | _LEGACY
                and isinstance(value.get("reason"), str)
                and all(
                    k in value and (value[k] is None or isinstance(value[k], str)) for k in FIELDS
                )
            )
            if valid and value["checked_at"] is not None:
                valid = datetime.fromisoformat(value["checked_at"]).tzinfo is not None
            if valid:
                state = {key: value[key] for key in self._empty()}
                if state["status"] in _LEGACY:
                    state.update(status="unknown", reason="")
                return state
        except FileNotFoundError:
            return self._empty()
        except OSError:
            return self._unreadable()
        except (ValueError, UnicodeError, TypeError):
            pass
        value = self._empty() | {
            "status": "corrupt",
            "reason": "健康状态记录损坏，重新启用账号即可",
        }
        try:
            write_json_atomic(path, value)
        except OSError:
            return self._unreadable()
        return value

    def _unreadable(self) -> dict[str, Any]:
        return self._empty() | {
            "status": "corrupt",
            "reason": "健康状态无法读取或保存，请检查文件和目录权限",
        }

    def view(self, name: str) -> dict[str, Any]:
        try:
            path = self._path(name)
            with self._locked(path):
                return self._view(self._read(path))
        except OSError:
            return self._view(self._unreadable())

    def record(self, name: str, status: str, reason: str, **fields: Any) -> dict[str, Any]:
        """Store the verdict of one live probe. ``status`` decides whether it blocks."""
        if status not in STATUSES or not isinstance(reason, str):
            raise ValueError("非法健康状态")
        if fields.keys() - FIELDS or any(
            v is not None and not isinstance(v, str) for v in fields.values()
        ):
            raise ValueError("非法健康字段")
        path = self._path(name)
        with self._locked(path):
            value = self._read(path)
            value.update(fields)
            value["status"] = status
            value["checked_at"] = fields.get("checked_at") or datetime.now(UTC).isoformat()
            value["reason"] = reason
            write_json_atomic(path, value)
            return self._view(value)

    def pause(self, name: str, reason: str, status: str) -> dict[str, Any]:
        """Record a problem a probe found; ``status`` names which problem it was."""
        if status not in NEEDS_USER | TRANSIENT:
            raise ValueError("非法暂停状态")
        return self.record(name, status, reason)

    def enable(self, name: str) -> dict[str, Any]:
        """Drop the cached verdict so the account may be tried again. Real sends never
        trust this cache: each one re-checks the session and the egress live."""
        path = self._path(name)
        with self._locked(path):
            value = self._read(path)
            value.update(status="unknown", reason="")
            write_json_atomic(path, value)
            return self._view(value)
