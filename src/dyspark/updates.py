"""Read-only version checks and narrowly scoped update requests for the GUI."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dyspark.update_source import BRANCH, command
from dyspark.update_state import (
    ACTIVE_PHASES,
    SHA,
    UpdateError,
    begin_maintenance,
    read_state,
    state_dir,
    write_state,
)

CHECK_SECONDS = 6 * 3600


def current_commit() -> str:
    package = Path(__file__).resolve().parent
    metadata = read_state(package / "_build.json")
    commit = metadata.get("commit")
    if isinstance(commit, str) and SHA.fullmatch(commit):
        return commit
    root = package.parent.parent
    if (root / ".git").exists():
        try:
            return command(["git", "rev-parse", "HEAD"], cwd=root, timeout=3)
        except UpdateError:
            pass
    return "unknown"


class UpdateService:
    def __init__(self, data: Path, *, idle: Callable[[], None], commit: str | None = None) -> None:
        self.data = data
        self.folder = state_dir(data)
        self.idle = idle
        self.commit = commit or current_commit()
        self.instance = os.environ.get("SPARK_UPDATE_INSTANCE", "")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._checking = threading.Lock()
        self._available: dict[str, Any] = {}
        self.on_shutdown: Callable[[], None] | None = None
        self.boot = os.environ.get("SPARK_UPDATE_BOOT", "")

    def managed(self) -> bool:
        heartbeat = read_state(self.folder / "heartbeat.json")
        timestamp = heartbeat.get("time")
        return bool(
            self.instance
            and heartbeat.get("instance") == self.instance
            and type(timestamp) in {int, float}
            and 0 <= time.time() - timestamp < 20
        )

    def status(self) -> dict[str, Any]:
        managed = self.managed()
        available = read_state(self.folder / "available.json") if managed else self._available
        transaction = read_state(self.folder / "transaction.json")
        maintenance = read_state(self.folder / "maintenance.json")
        pending = maintenance and maintenance.get("id") != transaction.get("id")
        phase = "queued" if pending else transaction.get("phase", "idle")
        latest = available.get("latest")
        can_update = bool(
            managed
            and available.get("current") == self.commit
            and isinstance(latest, str)
            and SHA.fullmatch(latest)
            and latest != self.commit
            and not available.get("error")
            and not maintenance
            and phase not in ACTIVE_PHASES
            and phase != "recovery_failed"
        )
        return {
            "current": self.commit,
            "latest": latest,
            "summary": available.get("summary", ""),
            "checked_at": available.get("checked_at"),
            "check_error": available.get("error"),
            "checking": self._checking.locked(),
            "managed": managed,
            "can_update": can_update,
            "phase": phase,
            "target": maintenance.get("target") if pending else transaction.get("target"),
            "error": transaction.get("error"),
            "request_id": maintenance.get("id") or transaction.get("id"),
        }

    def check(self) -> dict[str, Any]:
        if self.managed():
            write_state(self.folder / "check-request.json", {"id": secrets.token_hex(16)})
        else:
            self._wake.set()
        return self.status()

    def apply(self, target: str) -> dict[str, Any]:
        status = self.status()
        if not status["can_update"] or target != status["latest"]:
            raise UpdateError("更新未就绪或版本已变化，请重新检查；一键更新需要由更新管理器启动")
        request = {"id": secrets.token_hex(16), "instance": self.instance, "target": target}
        begin_maintenance(self.data, request, self.idle)
        return self.status()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=12)

    def _loop(self) -> None:
        next_check = 0.0
        while not self._stop.is_set():
            try:
                stop = read_state(self.folder / "stop.json")
                if (
                    self.instance
                    and self.boot
                    and stop == {"instance": self.instance, "boot": self.boot}
                ):
                    if self.on_shutdown is not None:
                        self.on_shutdown()
                    return
                if (time.monotonic() >= next_check or self._wake.is_set()) and not self.managed():
                    self._wake.clear()
                    self._check_public()
                    next_check = time.monotonic() + CHECK_SECONDS
            except UpdateError:
                pass  # API status surfaces corrupt state; never stop the existing service.
            self._stop.wait(0.5)

    def _check_public(self) -> None:
        if not self._checking.acquire(blocking=False):
            return
        try:
            request = urllib.request.Request(
                f"https://api.github.com/repos/langbyyi/douyin-spark/commits?sha={BRANCH}&per_page=1",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "dyspark-updater"},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(65537))
            if not isinstance(payload, list) or len(payload) != 1:
                raise ValueError("invalid commit list")
            payload = payload[0]
            latest = payload.get("sha")
            if not isinstance(latest, str) or not SHA.fullmatch(latest):
                raise ValueError("invalid commit")
            self._available = {
                "current": self.commit,
                "latest": latest,
                "summary": str(payload.get("commit", {}).get("message", "")).split("\n")[0][:300],
                "checked_at": time.time(),
                "error": None,
            }
        except urllib.error.HTTPError as exc:
            limited = exc.code == 429 or (
                exc.code == 403 and (exc.headers or {}).get("X-RateLimit-Remaining") == "0"
            )
            message = (
                "GitHub API 检查请求被限流，请稍后重试；更新管理器可通过 Git 检查"
                if limited
                else f"检查更新失败（GitHub HTTP {exc.code}），请稍后重试"
            )
            self._available = {**self._available, "error": message}
        except (OSError, ValueError, TypeError, AttributeError):
            self._available = {**self._available, "error": "检查更新失败，请检查网络后重试"}
        finally:
            self._checking.release()
