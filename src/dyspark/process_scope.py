"""Own the subprocess trees of a running service without touching other clients.

Managed children must use ``supervised_command`` and a private stdin pipe. Send
``b"start\\n"`` only after ``attach`` succeeds, so no command can escape ownership
between spawning its supervisor and assigning it to the service's scope.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SHUTDOWN_GRACE = 2.0
_PROC = Path("/proc")


def _process_identity(pid: int) -> str | None:
    """Distinguish a surviving group leader from a reused numeric PID."""
    if sys.platform.startswith("linux"):
        try:
            fields = (_PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            return fields[19]  # /proc starttime, measured from system boot
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
        return (result.stdout.strip() or None) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _group_has_other_processes(group: int) -> bool | None:
    """Ignore reaped-command zombies; an unknown inventory must not permit exit."""
    own_pid = os.getpid()
    if sys.platform.startswith("linux"):
        try:
            entries = list(_PROC.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == own_pid:
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                    return True
            except FileNotFoundError:
                continue  # A process exiting during the inventory is harmless.
            except (OSError, IndexError, ValueError):
                return None
        return False
    query = None
    try:
        query = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,pgid=,stat="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            close_fds=True,
        )
        stdout, _ = query.communicate(timeout=0.5)
        if query.returncode != 0:
            return None
        for line in stdout.splitlines():
            pid, pgid, state = line.split()
            # The completed ps query shared our group and is already gone.
            if (
                int(pid) not in {own_pid, query.pid}
                and int(pgid) == group
                and not state.startswith(("Z", "X"))
            ):
                return True
        return False
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    finally:
        if query is not None and query.poll() is None:
            query.kill()
            query.wait()


def supervised_command(argv: Sequence[str]) -> list[str]:
    """Wrap a command without modifying its arguments or interpreting a shell."""
    if not argv:
        raise ValueError("A supervised command cannot be empty")
    return [sys.executable, "-m", "dyspark.process_scope", "--supervise", "--", *argv]


def _node_preload_option(path: str) -> str:
    # NODE_OPTIONS accepts double-quoted arguments and backslash escapes; this
    # is parsed directly by Node and must not use shell-specific quoting.
    return "--require " + json.dumps(path, ensure_ascii=False)


def managed_environment() -> dict[str, str]:
    """Keep Playwright's POSIX browser children inside their managed task group."""
    environment = os.environ.copy()
    if os.name != "nt":
        hook = Path(__file__).with_name("managed_node.cjs").resolve()
        if not hook.is_file():
            raise OSError("Managed Node preload is missing")
        preload = _node_preload_option(str(hook))
        existing = environment.get("NODE_OPTIONS", "")
        environment["NODE_OPTIONS"] = f"{existing} {preload}" if existing else preload
    return environment


class _WindowsJob:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "SetHandleInformation": (
                [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(kernel32, name)
            function.argtypes, function.restype = argtypes, restype
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Children must never inherit a handle keeping this job alive.
            if not kernel32.SetHandleInformation(self._handle, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def attach(self, pid: int) -> None:
        # Windows 8+ permits nested jobs. Never enable BREAKAWAY_OK.
        handle = self._kernel32.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, handle):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(handle)

    def close(self) -> None:
        if self._handle:
            if not self._kernel32.CloseHandle(self._handle):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            self._handle = None


class _PosixGroups:
    def __init__(self) -> None:
        self._groups: dict[int, str | None] = {}

    def attach(self, pid: int) -> None:
        if os.getpgid(pid) != pid:
            raise OSError("Managed supervisors must start in their own process group")
        identity = _process_identity(pid)
        if identity is None:
            raise OSError("Cannot verify managed supervisor identity")
        self._groups[pid] = identity

    @staticmethod
    def _owns_group(group: int, identity: str | None) -> bool:
        try:
            if os.getpgid(group) != group:
                return False
        except ProcessLookupError:
            return True  # The original group can outlive its leader.
        current_identity = _process_identity(group)
        if identity is None or current_identity is None:
            raise OSError("Cannot verify managed supervisor identity")
        return identity == current_identity

    def close(self) -> None:
        groups = dict(self._groups)
        live_groups = []
        for group, identity in groups.items():
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                self._groups.pop(group, None)
                continue
            if not self._owns_group(group, identity):
                self._groups.pop(group, None)
                continue
            try:
                os.killpg(group, signal.SIGTERM)
                live_groups.append(group)
            except ProcessLookupError:
                self._groups.pop(group, None)
        if live_groups:
            time.sleep(_SHUTDOWN_GRACE + 0.2)
        for group in live_groups:
            if not self._owns_group(group, groups[group]):
                self._groups.pop(group, None)
                continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
            self._groups.pop(group, None)


def _finish_posix_group() -> None:
    """Exit only after descendants are gone, including those ignoring TERM."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    group = os.getpgrp()
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + _SHUTDOWN_GRACE
    clean_inventory = False
    while time.monotonic() < deadline:
        if _group_has_other_processes(group) is False:
            if clean_inventory:
                return
            clean_inventory = True
        else:
            clean_inventory = False
        time.sleep(0.05)
    # Keeping the supervisor alive until here preserves its pipe watchdog.
    # If cleanup cannot be verified, force the whole group down, including us.
    os.killpg(group, signal.SIGKILL)


class ProcessScope:
    """Service-owned processes; closing the scope terminates all attached trees."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._backend: Any = _WindowsJob() if os.name == "nt" else _PosixGroups()

    def attach(self, pid: int) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Process scope is closed")
            if type(pid) is not int or pid <= 0:
                raise ValueError("Process ID must be a positive integer")
            self._backend.attach(pid)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._backend.close()
            self._closed = True


def _supervise(argv: Sequence[str]) -> int:
    if sys.stdin.buffer.readline() != b"start\n":
        return 0
    if not argv:
        return 2
    stop = threading.Event()
    posix = os.name != "nt"
    if posix:
        # The supervisor shares its group with the command and its descendants.
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    else:
        # Keep a second job handle inside the supervisor itself. Its forced exit
        # releases this handle and kills descendants even if another GUI/MCP
        # process still owns the outer service job. Attach before spawning.
        child_scope = _WindowsJob()
        child_scope.attach(os.getpid())
    child = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, close_fds=True, env=managed_environment()
    )

    def parent_watch() -> None:
        try:
            # A blocked BufferedReader daemon holds its lock during interpreter
            # shutdown. Raw fd reads let a normally finished command exit cleanly.
            while os.read(sys.stdin.fileno(), 1):
                pass
        except OSError:
            pass
        finally:
            stop.set()

    threading.Thread(target=parent_watch, name="service-parent-watch", daemon=True).start()
    while child.poll() is None:
        if stop.wait(0.05):
            if posix:
                _finish_posix_group()
            else:
                child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=_SHUTDOWN_GRACE)
                if child.poll() is None:
                    child.kill()
            break
    code = child.wait()
    if posix:
        _finish_posix_group()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[:2] != ["--supervise", "--"]:
        return 2
    return _supervise(args[2:])


if __name__ == "__main__":
    raise SystemExit(main())
