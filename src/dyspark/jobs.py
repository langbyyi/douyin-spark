"""Background ``spark`` processes started on behalf of the MCP server / GUI.

A send can take minutes (random gaps between friends) and a login waits for a
human, so tools never run them inline: each job is a separate CLI process with a
private folder ``data/jobs/<id>/``:

  job.json      what was started and the process id
  output.log    the child's console output
  result.json   progress and final result, written by the child (``--job-dir``)
  session.json  web login status (login jobs), next to qr.png / screen.png
  cancel        created to ask a login job to stop

GUI/MCP services contain their own children and reclaim them on shutdown.
Their persisted state remains available to later sessions. Standalone managers
can still launch independent one-shot CLI jobs.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from dyspark.log_text import mask_log_secrets
from dyspark.runtime import needs_display
from dyspark.store import DataDir, ensure_private_dir, pid_alive, read_json, write_json_atomic
from dyspark.update_state import UpdateError
from dyspark.update_state import operation as update_operation

KINDS = ("run", "check", "login", "friends")
JOB_ID = re.compile(r"^(run|check|login|friends)-\d{8}-\d{6}-[0-9a-f]{6}$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
MAX_WAIT_SECONDS = 55.0  # stay below the common 60 s tool-call timeout


class JobError(RuntimeError):
    """Unknown job id or a job that cannot be started."""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: str
    directory: Path

    def file(self, name: str) -> Path:
        return self.directory / name


def job_module() -> str:
    """Module run as ``python -m <module>``; tests point it at their fake site."""
    module = os.environ.get("SPARK_JOB_MODULE", "dyspark")
    return module if _MODULE.match(module) else "dyspark"


def needs_virtual_display() -> bool:
    return needs_display() and shutil.which("xvfb-run") is not None


def tail(path: Path, lines: int = 40, *, strict: bool = False) -> list[str]:
    """Read a bounded suffix, without loading an entire service log into memory."""
    if lines <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            chunks = []
            size = breaks = 0
            while position and breaks <= lines and size < 1_048_576:
                length = min(position, 8192, 1_048_576 - size)
                position -= length
                stream.seek(position)
                chunk = stream.read(length)
                chunks.append(chunk)
                breaks += chunk.count(b"\n")
                size += len(chunk)
            suffix = b"".join(reversed(chunks))
        if position:
            # The first line starts before the suffix, including possibly a UTF-8 character.
            boundary = suffix.find(b"\n")
            if boundary >= 0 and not (size >= 1_048_576 and breaks <= lines):
                suffix = suffix[boundary + 1 :]
            else:
                suffix = "…单条日志过长，已截断前部…".encode() + suffix.lstrip(
                    bytes(range(0x80, 0xC0))
                )
        text = suffix.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    return [mask_log_secrets(line) for line in text.splitlines()[-lines:]]


@contextlib.contextmanager
def _windows_process(pid: int, *, terminate: bool = False) -> Iterator[tuple[Any, Any]]:
    """Keep one handle open while verifying identity and (optionally) terminating."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    access = 0x1000 | (0x0001 if terminate else 0)  # query limited information / terminate
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield kernel32, handle
    finally:
        kernel32.CloseHandle(handle)


def _windows_created_at(kernel32: Any, handle: Any) -> str:
    import ctypes
    from ctypes import wintypes

    creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)


def _process_created_at(pid: int) -> str | None:
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        with contextlib.suppress(OSError), _windows_process(pid) as (kernel32, handle):
            return _windows_created_at(kernel32, handle)
    elif sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, IndexError):
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:{boot}:{fields[19]}"
    elif sys.platform == "darwin":
        try:
            query = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
                check=True,
                timeout=2,
            )
            weekday, month, day, clock, year = query.stdout.split()
            if weekday not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
                return None
            months = (
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            )
            hour, minute, second = (int(part) for part in clock.split(":"))
            created = datetime(int(year), months.index(month) + 1, int(day), hour, minute, second)
            return "darwin:" + created.isoformat(timespec="seconds")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return None


def _metadata(path: Path, errors: dict[str, str], *, required: bool = False) -> dict[str, Any]:
    try:
        value = read_json(path, None)
        if value is None:
            if required or path.exists() or path.with_name(path.name + ".corrupt").exists():
                errors[path.name] = "元数据缺失或损坏"
            return {}
        if not isinstance(value, dict) or not value:
            errors[path.name] = "元数据格式错误"
            return {}
        return value
    except OSError as exc:
        errors[path.name] = f"元数据读取失败：{exc}"
        return {}


class JobManager:
    def __init__(self, data: DataDir, config_path: Path, zone: tzinfo) -> None:
        self.data = data
        self.config_path = config_path
        self.zone = zone
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._managed = False
        self._scopes: dict[str, Any] = {}
        self._closed = False
        self._shutdown_complete = False
        self._lifecycle_lock = threading.RLock()

    def enable_service(self) -> None:
        """Contain only children launched by this manager while its service is alive."""
        with self._lifecycle_lock:
            if self._closed:
                raise JobError("任务服务已停止")
            self._managed = True

    def shutdown(self, grace_s: float = 2) -> None:
        """Cancel this service's own children and close their process containment."""
        with self._lifecycle_lock:
            self._shutdown(grace_s)

    def _shutdown(self, grace_s: float) -> None:
        if self._shutdown_complete:
            return
        self._closed = True
        if not self._managed:
            self._shutdown_complete = True
            return  # standalone managers do not claim independently launched CLI jobs
        for job_id, process in self._procs.items():
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    (self.data.jobs_dir / job_id / "cancel").touch()
        deadline = time.monotonic() + max(0, grace_s)
        while any(p.poll() is None for p in self._procs.values()) and time.monotonic() < deadline:
            time.sleep(0.05)
        errors = []
        for scope in self._scopes.values():
            try:
                scope.close()
            except OSError as exc:
                errors.append(str(exc))
        for process in self._procs.values():
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                errors.append(f"服务任务进程 {process.pid} 尚未退出，请检查")
        if errors:
            raise JobError("；".join(errors))
        self._shutdown_complete = True

    # --------------------------------------------------------------- start
    def command(self, job: Job, spark_args: list[str]) -> list[str]:
        argv = [
            sys.executable,
            "-m",
            job_module(),
            "--config",
            str(self.config_path),
            "--data",
            str(self.data.root),
            "--job-dir",
            str(job.directory),
            *spark_args,
        ]
        if needs_virtual_display():
            screen = "-screen 0 1366x900x24 -nolisten tcp"
            argv = ["xvfb-run", "--auto-servernum", f"--server-args={screen}", *argv]
        return argv

    def start(self, kind: str, spark_args: list[str]) -> Job:
        try:
            with update_operation(self.data.root), self._lifecycle_lock:
                return self._start_job(kind, spark_args)
        except UpdateError as exc:
            raise JobError(str(exc)) from exc

    def _start_job(self, kind: str, spark_args: list[str]) -> Job:
        if self._closed:
            raise JobError("任务服务已停止，不能启动新任务")
        self._reap_scopes()
        if kind not in KINDS:
            raise JobError(f"未知任务类型：{kind!r}")
        stamp = datetime.now(self.zone).strftime("%Y%m%d-%H%M%S")
        job_id = f"{kind}-{stamp}-{secrets.token_hex(3)}"
        job = Job(job_id, kind, ensure_private_dir(self.data.jobs_dir / job_id))
        argv = self.command(job, spark_args)
        if self._managed:
            from dyspark.process_scope import supervised_command

            argv = supervised_command(argv)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            options["start_new_session"] = True  # isolate this task's subprocess group
        with job.file("output.log").open("ab") as output:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if self._managed else subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_path.parent),
                    env=env,
                    **options,
                )
            except OSError as exc:
                raise JobError(f"无法启动后台任务：{exc}") from exc
        self._procs[job.id] = process
        try:
            if self._managed:
                from dyspark.process_scope import ProcessScope

                scope = ProcessScope()
                self._scopes[job.id] = scope
                scope.attach(process.pid)
                assert process.stdin is not None
                process.stdin.write(b"start\n")
                process.stdin.flush()
            write_json_atomic(
                job.file("job.json"),
                {
                    "id": job.id,
                    "kind": kind,
                    "args": spark_args,
                    "pid": process.pid,
                    "process_created_at": _process_created_at(process.pid),
                    "host": socket.gethostname(),
                    "started_at": datetime.now(self.zone).isoformat(timespec="seconds"),
                },
            )
        except Exception as exc:
            scope = self._scopes.pop(job.id, None)
            if scope is not None:
                try:
                    scope.close()
                except OSError as cleanup:
                    self._scopes[job.id] = scope
                    raise JobError(f"任务 {job.id} 无法回收其进程组：{cleanup}") from exc
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as cleanup:
                    raise JobError(
                        f"任务 {job.id} 元数据写入失败，进程 {process.pid} 无法回收，"
                        f"需要人工核对：{exc}；{cleanup}"
                    ) from exc
            self._procs.pop(job.id, None)
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            raise JobError(f"任务 {job.id} 元数据写入失败，已停止子进程：{exc}") from exc
        return job

    # ---------------------------------------------------------------- query
    def _reap_scopes(self) -> None:
        with self._lifecycle_lock:
            self._reap_finished_scopes()

    def _reap_finished_scopes(self) -> None:
        for job_id, scope in list(self._scopes.items()):
            process = self._procs[job_id]
            if process.poll() is not None:
                scope.close()
                self._scopes.pop(job_id, None)
                if process.stdin is not None:
                    with contextlib.suppress(OSError):
                        process.stdin.close()

    def get(self, job_id: str) -> Job:
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            raise JobError(f"任务编号格式不对：{job_id!r}")
        directory = self.data.root / "jobs" / job_id
        if not (directory / "job.json").is_file():
            raise JobError(f"找不到任务 {job_id}（可能已被清理）")
        return Job(job_id, job_id.split("-", 1)[0], directory)

    def _exit_code(self, job: Job, meta: dict[str, Any]) -> tuple[bool, int | None]:
        """``(alive, exit code if known)`` of the job's process."""
        process = self._procs.get(job.id)
        if process is not None:
            code = process.poll()
            return code is None, code
        pid = meta.get("pid")
        if meta.get("host") == socket.gethostname() and type(pid) is int and pid > 0:
            if not pid_alive(pid):
                return False, None
            expected = meta.get("process_created_at")
            actual = _process_created_at(pid)
            if not isinstance(expected, str) or not expected or actual is None:
                raise JobError("任务进程身份未能验证，需要人工核对；禁止仅按存活进程编号判断")
            return actual == expected, None
        return False, None

    def describe(self, job: Job, output_lines: int = 15) -> dict[str, Any]:
        errors: dict[str, str] = {}
        meta = _metadata(job.file("job.json"), errors, required=True)
        result = _metadata(job.file("result.json"), errors)
        session = _metadata(job.file("session.json"), errors)
        if result and (
            result.get("state") not in ("running", "finished")
            or (result.get("state") == "finished" and type(result.get("exit_code")) is not int)
            or (result.get("data") is not None and not isinstance(result.get("data"), dict))
        ):
            errors["result.json"] = "任务结果格式错误，不能确认完成状态"
        alive, code = False, None
        if result.get("state") != "finished":
            try:
                alive, code = self._exit_code(job, meta)
            except JobError as exc:
                errors["process_identity"] = str(exc)
        if result.get("state") == "finished":
            state, exit_code = "finished", result.get("exit_code")
        elif alive:
            state, exit_code = "running", None
        else:
            state, exit_code = "failed", code
        info: dict[str, Any] = {
            "job_id": job.id,
            "kind": job.kind,
            "state": state,
            "exit_code": exit_code,
            "started_at": meta.get("started_at"),
            "args": meta.get("args"),
            "result": result.get("data"),
        }
        if session:
            info["session"] = session
        if errors:
            info.update(
                state="blocked",
                exit_code=None,
                blocked=True,
                metadata_errors=errors,
                message="任务状态待核对，已阻止新任务启动："
                + "；".join(f"{name}：{error}" for name, error in errors.items()),
            )
        if state != "running" and exit_code != 0:
            info["output_tail"] = tail(job.file("output.log"), output_lines)
        if not errors and state == "failed" and not result:
            info["message"] = "任务进程已退出但没有写出结果，请查看 output_tail"
        return info

    def wait(
        self,
        job: Job,
        seconds: float,
        until: Callable[[dict[str, Any]], bool] | None = None,
        poll_s: float = 0.5,
    ) -> dict[str, Any]:
        """Describe the job once it ends, ``until`` holds, or ``seconds`` pass."""
        deadline = time.monotonic() + max(0.0, min(seconds, MAX_WAIT_SECONDS))
        while True:
            info = self.describe(job)
            if info["state"] != "running" or (until and until(info)):
                return info
            if time.monotonic() >= deadline:
                return info
            time.sleep(poll_s)

    def recent(self, limit: int | None = 10) -> list[dict[str, Any]]:
        self._reap_scopes()
        folders = sorted(
            (p for p in (self.data.root / "jobs").glob("*-*") if JOB_ID.match(p.name)),
            key=lambda p: p.name.split("-", 1)[1],
            reverse=True,
        )
        summaries = []
        for folder in folders[:limit]:
            info = self.describe(Job(folder.name, folder.name.split("-", 1)[0], folder))
            summaries.append(
                {
                    k: info.get(k)
                    for k in (
                        "job_id",
                        "kind",
                        "state",
                        "exit_code",
                        "started_at",
                        "args",
                        "blocked",
                        "message",
                        "metadata_errors",
                    )
                }
            )
        return summaries

    def active(self) -> list[dict[str, Any]]:
        # Display limits must not hide a surviving child or an inspection hold.
        return [job for job in self.recent(None) if job["state"] in {"running", "blocked"}]

    # --------------------------------------------------------------- cancel
    def cancel(self, job: Job, grace_s: float = 12) -> dict[str, Any]:
        """Ask the job to stop (cancel file), then terminate it after ``grace_s``."""
        info = self.describe(job)
        if self._login_saving(job, info):
            return info
        job.file("cancel").touch()
        deadline = time.monotonic() + max(0, grace_s)
        while True:
            info = self.describe(job)
            if self._login_saving(job, info):
                return info
            if info["state"] == "finished":
                return info
            errors: dict[str, str] = {}
            meta = _metadata(job.file("job.json"), errors, required=True)
            if errors and job.id not in self._procs:
                raise JobError("无法验证任务进程身份，拒绝强制结束；请核对原任务进程")
            alive, _ = self._exit_code(job, meta)
            if not alive:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
        info = self.describe(job)
        if self._login_saving(job, info):
            return info
        self._terminate(job)
        return self.wait(job, 5)

    @staticmethod
    def _login_saving(job: Job, info: dict[str, Any]) -> bool:
        return job.kind == "login" and (info.get("session") or {}).get("status") in {
            "finalizing",
            "success",
        }

    def _terminate(self, job: Job) -> None:
        with self._lifecycle_lock:
            scope = self._scopes.get(job.id)
            if scope is not None:
                scope.close()
                self._scopes.pop(job.id, None)
                return
        process = self._procs.get(job.id)
        meta = read_json(job.file("job.json"), {})
        pid = process.pid if process else meta.get("pid") if isinstance(meta, dict) else None
        if os.name == "nt":
            if process is not None:
                # terminate() only kills the CLI child; the browser and driver survive
                # as orphans. taskkill /T is the Windows counterpart of the POSIX
                # process-group kill below (the cancel-file grace already elapsed).
                with contextlib.suppress(OSError):
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                return
            expected = meta.get("process_created_at") if isinstance(meta, dict) else None
            if (
                not isinstance(pid, int)
                or pid <= 0
                or meta.get("host") != socket.gethostname()
                or not isinstance(expected, str)
                or not expected.isdigit()
            ):
                raise JobError("无法验证任务进程身份，拒绝强制结束；请核对原任务进程")
            try:
                with _windows_process(pid, terminate=True) as (kernel32, handle):
                    if _windows_created_at(kernel32, handle) != expected:
                        raise JobError("任务进程身份已变化，拒绝结束可能复用编号的进程")
                    if not kernel32.TerminateProcess(handle, 1):
                        import ctypes

                        raise ctypes.WinError(ctypes.get_last_error())
            except OSError as exc:
                raise JobError(f"无法验证或结束任务进程身份：{exc}") from exc
            return
        if not isinstance(pid, int):
            return
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)  # the job leads its own process group


class JobReporter:
    """Child side: writes ``result.json`` for the MCP server (no-op without a job dir)."""

    def __init__(self, directory: Path | None, command: str, zone: tzinfo) -> None:
        self.directory = directory
        self.command = command
        self.zone = zone
        self.data: dict[str, Any] = {}

    def progress(self, data: dict[str, Any]) -> None:
        self.data = data
        self._write("running", None)

    def finish(self, exit_code: int, data: dict[str, Any] | None = None) -> None:
        if data is not None:
            self.data = data
        self._write("finished", exit_code)

    def _write(self, state: str, exit_code: int | None) -> None:
        if self.directory is None:
            return
        with contextlib.suppress(OSError):
            write_json_atomic(
                self.directory / "result.json",
                {
                    "state": state,
                    "command": self.command,
                    "exit_code": exit_code,
                    "updated_at": datetime.now(self.zone).isoformat(timespec="seconds"),
                    "data": self.data,
                },
            )
