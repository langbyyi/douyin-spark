"""Single-owner, persistent daily scheduling for the running control panel."""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dyspark.schedule_time import due_at as _due
from dyspark.store import (
    RunBusyError,
    ensure_private_dir,
    exclusive_run,
    read_json,
    write_json_atomic,
)
from dyspark.update_state import UpdateError, ensure_available

log = logging.getLogger(__name__)
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS = 3
MAX_SLOTS = 4096


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("调度状态中的时间缺少时区")
    return instant


def _after(now: datetime, delay: timedelta) -> datetime:
    return datetime.fromtimestamp(now.timestamp() + delay.total_seconds(), now.tzinfo)


def _busy(error: Exception) -> bool:
    return isinstance(error, RunBusyError) or any(
        text in str(error)
        for text in (
            "已有任务在运行",
            "另一个 dyspark",
            "busy",
            "正在使用数据目录",
            "程序正在更新",
            "更新或其他操作",
        )
    )


def _retryable(info: dict[str, Any]) -> bool:
    if info.get("state") == "blocked" or info.get("blocked"):
        return False
    if info.get("state") != "finished":
        return True
    result = info.get("result") or {}
    reports = result.get("accounts", result.get("reports", []))
    if reports:
        # A paused account must not prevent retrying another account's ordinary failure.
        return any(report.get("verdict") in {"partial", "error"} for report in reports)
    return info.get("exit_code") not in (0, 2, 3)


class Scheduler:
    """Uses the service backend's guarded and contained job launcher.

    ``tick`` also works without ``start`` for deterministic embedders/tests, acquiring
    the same owner lock for that single tick. A service holds it in its worker thread.
    """

    def __init__(
        self,
        backend: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        dispatch: Callable[..., dict[str, Any]] | None = None,
        jitter: Callable[[float], float] | None = None,
        poll_seconds: float = 1,
    ) -> None:
        self.backend = backend
        self._clock = clock
        self._dispatch = dispatch or backend.run
        self._jitter = jitter or (lambda maximum: random.uniform(0, maximum))
        self._poll_seconds = max(0.01, poll_seconds)
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._root: Path | None = None
        self._state: dict[str, Any] = {"slots": {}, "last_trigger": None}
        self._view: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "leader": False,
            "last_trigger": None,
            "next_run": None,
            "active_job": None,
            "error": None,
            "started_at": None,
            "heartbeat_at": None,
        }

    def _now(self, settings: Any, now: datetime | None = None) -> datetime:
        value = now or (self._clock() if self._clock else datetime.now(settings.zone))
        if value.tzinfo is None:
            value = value.replace(tzinfo=settings.zone)
        return value.astimezone(settings.zone)

    def start(self) -> None:
        with self._mutex:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="dyspark-scheduler",
                daemon=True,
            )
            self._thread.start()
        self._ready.wait(2)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("调度线程未能在服务停止时退出")

    def _loop(self) -> None:
        with self._mutex:
            self._view["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    settings = self.backend.settings()
                    with self._mutex:
                        heartbeat = self._now(settings).isoformat()
                        self._view["started_at"] = self._view["started_at"] or heartbeat
                        self._view["heartbeat_at"] = heartbeat
                    root = ensure_private_dir(Path(settings.data_dir).resolve())
                    with exclusive_run(root / "scheduler.running"):
                        with self._mutex:
                            self._root = root
                            self._view["leader"] = True
                            self._view["started_at"] = self._now(settings).isoformat()
                        while not self._stop.is_set():
                            self.tick()
                            self._ready.set()
                            self._stop.wait(self._poll_seconds)
                except RunBusyError:
                    with self._mutex:
                        self._view["leader"] = False
                        self._view["error"] = "另一个面板进程持有自动调度器"
                except Exception as exc:
                    with self._mutex:
                        self._view["error"] = str(exc)
                    log.warning("scheduler unavailable: %s", exc)
                finally:
                    with self._mutex:
                        self._view["leader"] = False
                    self._ready.set()
                self._stop.wait(self._poll_seconds)
        finally:
            with self._mutex:
                self._view["running"] = False
                self._view["leader"] = False

    def tick(self, now: datetime | None = None) -> None:
        with self._mutex:
            try:
                settings = self.backend.settings()
                instant = self._now(settings, now)
                self._view["enabled"] = getattr(settings, "schedule_enabled", False)
                self._view["heartbeat_at"] = instant.isoformat()
                root = ensure_private_dir(Path(settings.data_dir).resolve())
                if self._view["leader"]:
                    if root != self._root:
                        raise ValueError("运行中不能更换自动调度的数据目录，请重启面板")
                    self._tick(settings, instant)
                else:
                    with exclusive_run(root / "scheduler.running"):
                        self._root = root
                        self._tick(settings, instant)
            except RunBusyError:
                self._view["error"] = "另一个面板进程持有自动调度器"
            except Exception as exc:
                self._view["error"] = str(exc)
                self._view["next_run"] = None
                log.warning("scheduler tick failed: %s", exc)

    def status(self) -> dict[str, Any]:
        with self._mutex:
            view = self._view.copy()
            try:
                view["enabled"] = getattr(self.backend.settings(), "schedule_enabled", False)
                if not view["enabled"]:
                    view["next_run"] = None
            except Exception as exc:
                view["error"] = str(exc)
            return view

    def _save(self) -> None:
        assert self._root is not None
        write_json_atomic(self._root / "scheduler.json", self._state)

    def _load(self) -> dict[str, Any]:
        assert self._root is not None
        path = self._root / "scheduler.json"
        state = read_json(path, None)
        if (
            state is None
            and not path.with_name(path.name + ".corrupt").exists()
            and not path.exists()
        ):
            return {"slots": {}, "last_trigger": None}
        try:
            if not isinstance(state, dict) or not isinstance(state.get("slots"), dict):
                raise ValueError
            for key, slot in state["slots"].items():
                if not isinstance(slot, dict):
                    raise ValueError
                date.fromisoformat(slot["date"])
                datetime.strptime(slot["time"], "%H:%M")
                if not isinstance(slot["zone"], str) or key != (
                    f"{slot['date']}|{slot['zone']}|{slot['time']}"
                ):
                    raise ValueError
                _instant(slot["due_at"])
                _instant(slot["eligible_at"])
                if type(slot["attempts"]) is not int or not 0 <= slot["attempts"] <= MAX_ATTEMPTS:
                    raise ValueError
                if slot["state"] not in {
                    "pending",
                    "launching",
                    "running",
                    "blocked",
                    "done",
                    "cancelled",
                }:
                    raise ValueError
                if slot.get("job_id") is not None and not isinstance(slot["job_id"], str):
                    raise ValueError
                if slot["state"] in {"launching", "running", "blocked"}:
                    _instant(slot["last_attempt"])
                    if not slot["attempts"] or (
                        slot["state"] in {"running", "blocked"} and not slot.get("job_id")
                    ):
                        raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("调度状态损坏，需要核对 scheduler.json 及其 .corrupt 备份") from exc
        return state

    def _prune(self, now: datetime, settings: Any) -> None:
        slots = self._state["slots"]
        cutoff = now.date() - timedelta(days=max(1, min(30, getattr(settings, "keep_days", 7))))
        disposable = sorted(
            (
                key
                for key, slot in slots.items()
                if slot["state"] not in {"running", "launching", "blocked"}
            ),
        )
        removed = False
        for key in disposable:
            slot_day = date.fromisoformat(slots[key]["date"])
            if slot_day < cutoff or (len(slots) > MAX_SLOTS and slot_day < now.date()):
                del slots[key]
                removed = True
        if removed:
            self._save()

    @staticmethod
    def _key(due: datetime, time: str, settings: Any) -> str:
        return f"{due.date().isoformat()}|{settings.zone}|{time}"

    def _finish(self, slot: dict[str, Any], info: dict[str, Any], now: datetime) -> None:
        if info.get("state") == "blocked" or info.get("blocked"):
            slot["state"] = "blocked"
            slot["last_result"] = {"state": "blocked", "message": info.get("message")}
            self._view["error"] = info.get("message") or "任务状态待核对，自动计划已阻止触发"
            self._view["active_job"] = slot.get("job_id")
            self._save()
            return
        if info.get("state") == "running":
            slot["state"] = "running"
            return
        slot["job_id"] = None
        result = info.get("result") or {}
        if info.get("exit_code") == 75 or result.get("busy"):
            slot["attempts"] = max(0, slot["attempts"] - 1)
            slot["state"] = "pending"
            slot["eligible_at"] = _after(now, timedelta(seconds=self._poll_seconds)).isoformat()
        elif _retryable(info) and slot["attempts"] < MAX_ATTEMPTS:
            slot["state"] = "pending"
            slot["eligible_at"] = _after(now, RETRY_DELAY).isoformat()
        else:
            slot["state"] = "done"
        slot["last_result"] = {
            "state": info.get("state"),
            "exit_code": info.get("exit_code"),
        }
        self._save()

    def _next_run(self, settings: Any, now: datetime) -> str | None:
        eligibility = [
            _instant(slot["eligible_at"])
            for slot in self._state["slots"].values()
            if slot["state"] == "pending"
            and slot["attempts"] < MAX_ATTEMPTS
            and _instant(slot["eligible_at"]).astimezone(settings.zone).date() == now.date()
        ]
        eligibility.extend(
            due
            for time in settings.schedule_times
            if (due := _due(now.date(), time, settings.zone)) and due.timestamp() > now.timestamp()
        )
        day = now.date()
        while not eligibility and settings.schedule_times:
            day += timedelta(days=1)
            eligibility.extend(
                due for time in settings.schedule_times if (due := _due(day, time, settings.zone))
            )
        if not eligibility:
            return None
        earliest = min(eligibility, key=lambda instant: instant.timestamp())
        return max((now, earliest), key=lambda instant: instant.timestamp()).isoformat()

    def _tick(self, settings: Any, now: datetime) -> None:
        assert self._root is not None
        try:
            ensure_available(settings.data_dir)
        except UpdateError:
            self._view.update(error=None, next_run=None, active_job=None)
            return
        self._state = self._load()
        self._prune(now, settings)
        slots = self._state["slots"]
        self._view.update(error=None, next_run=None, active_job=None)
        self._view["last_trigger"] = self._state.get("last_trigger")
        times = sorted(set(settings.schedule_times))
        # Read surviving children before reserving any new trigger after a restart.
        active = self.backend.jobs(settings).active()
        if active:
            self._view["active_job"] = active[0]["job_id"]
            for job in active:
                if job.get("state") == "blocked" or job.get("blocked"):
                    self._view["error"] = job.get("message") or "任务状态待核对，自动计划已阻止触发"
                    self._view["active_job"] = job["job_id"]
                    break
        for slot in slots.values():
            identifier = slot.get("job_id")
            if slot.get("state") in {"running", "blocked"} and identifier:
                try:
                    info = self.backend.run_status(identifier, 0)
                except Exception as exc:
                    info = {"state": "failed", "exit_code": None, "message": str(exc)}
                self._finish(slot, info, now)
                if info.get("state") == "running":
                    self._view["active_job"] = identifier
            elif slot.get("state") == "launching":
                # Reservation written before launch: child may have outlived its panel.
                slot["state"] = "pending"
                slot["eligible_at"] = (
                    _after(_instant(slot["last_attempt"]), RETRY_DELAY)
                ).isoformat()
                self._save()
        if not self._view["enabled"] or self._view["error"]:
            return
        for slot in slots.values():
            age = now.timestamp() - _instant(slot["due_at"]).timestamp()
            expired = slot["zone"] == str(settings.zone) and slot["date"] != now.date().isoformat()
            changed = slot["time"] not in times or slot["zone"] != str(settings.zone)
            if slot["state"] == "pending" and slot["attempts"] >= MAX_ATTEMPTS:
                slot["state"] = "done"
                self._save()
            elif slot["state"] == "pending" and (expired or changed):
                slot["state"] = "cancelled"
                slot["cancel_reason"] = "expired" if expired else "config_changed"
                self._save()
            elif (
                slot["state"] == "cancelled"
                and slot.get("cancel_reason") == "expired"
                and slot["attempts"] < MAX_ATTEMPTS
                and not slot.get("job_id")
                and not changed
                and not expired
                and age >= 0
            ):
                # Older versions expired same-day retries after just two hours.
                slot["state"] = "pending"
                slot.pop("cancel_reason", None)
                self._save()
            elif (
                slot["state"] == "cancelled"
                and slot.get("cancel_reason") == "config_changed"
                and slot["attempts"] == 0
                and not slot.get("job_id")
                and not slot.get("last_attempt")
                and not changed
                and not expired
                and age >= 0
            ):
                slot["state"] = "pending"
                slot.pop("cancel_reason", None)
                self._save()
        due_times = [due for time in times if (due := _due(now.date(), time, settings.zone))]
        catchup = [due for due in due_times if due.timestamp() <= now.timestamp()]
        if catchup:
            due = max(catchup, key=lambda instant: instant.timestamp())
            for slot in slots.values():
                if (
                    slot["state"] == "pending"
                    and slot["attempts"] == 0
                    and _instant(slot["due_at"]).timestamp() < due.timestamp()
                ):
                    # A busy/paused service may retain several never-started slots.
                    # Only the latest is catch-up work; actual retries stay pending.
                    slot["state"] = "cancelled"
                    slot["cancel_reason"] = "superseded"
                    self._save()
            time = due.strftime("%H:%M")
            key = self._key(due, time, settings)
            if key not in slots:
                if len(slots) >= MAX_SLOTS:
                    raise ValueError("调度状态记录过多，需要核对 scheduler.json")
                maximum = settings.timing.jitter_minutes * 60
                delay = max(0, min(maximum, self._jitter(maximum)))
                slots[key] = {
                    "date": due.date().isoformat(),
                    "time": time,
                    "zone": str(settings.zone),
                    "due_at": due.isoformat(),
                    "eligible_at": _after(due, timedelta(seconds=delay)).isoformat(),
                    "attempts": 0,
                    "state": "pending",
                    "job_id": None,
                }
                self._save()
        pending = sorted(
            (
                slot
                for slot in slots.values()
                if slot["state"] == "pending" and slot["attempts"] < MAX_ATTEMPTS
            ),
            key=lambda slot: _instant(slot["eligible_at"]).timestamp(),
        )
        self._view["next_run"] = self._next_run(settings, now)
        ready = [slot for slot in pending if _instant(slot["eligible_at"]) <= now]
        if not ready or active or self._view["active_job"] or self._stop.is_set():
            return
        slot = ready[0]
        slot["state"] = "launching"
        slot["attempts"] += 1
        slot["last_attempt"] = now.isoformat()
        self._save()  # durable reservation before the backend creates a process
        try:
            info = self._dispatch(
                account=None,
                dry_run=False,
                wait_seconds=0,
                trigger="scheduler",
            )
        except Exception as exc:
            slot["state"] = "pending"
            if _busy(exc):
                slot["attempts"] -= 1
                slot["eligible_at"] = now.isoformat()
            else:
                slot["eligible_at"] = _after(now, RETRY_DELAY).isoformat()
                if slot["attempts"] >= MAX_ATTEMPTS:
                    slot["state"] = "done"
            slot["last_result"] = {"state": "failed", "message": str(exc)}
            self._view["error"] = str(exc)
            self._view["next_run"] = self._next_run(settings, now)
            self._save()
            return
        slot["job_id"] = info["job_id"]
        slot["state"] = "running"
        self._state["last_trigger"] = now.isoformat()
        self._view["last_trigger"] = now.isoformat()
        self._view["active_job"] = info["job_id"]
        self._save()  # record the surviving child before another trigger can launch
        self._finish(slot, info, now)
        self._view["next_run"] = None if self._view["error"] else self._next_run(settings, now)
        if info.get("state") not in {"running", "blocked"} and not info.get("blocked"):
            self._view["active_job"] = None
