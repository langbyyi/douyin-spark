"""On-disk state: per-account login records, the daily send ledger and run artefacts.

Layout under the data directory::

    accounts/<name>/profile/      persistent browser profile (never copied elsewhere)
    accounts/<name>/login.json    where/how the account was logged in
    ledger/<YYYY-MM-DD>.json      per-day send results, used to skip finished friends
    runs/<YYYYmmdd-HHMMSS>/       log, screenshots and raw responses of one invocation
    logs/<service>.log*           size-rotated logs of the resident gui/mcp services
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import MISSING, asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dyspark.locking import LockBusyError, file_lock
from dyspark.names import normalize_name
from dyspark.receipt import Receipt

log = logging.getLogger(__name__)

PRIVATE_DIR_MODE = 0o700
RUN_MARKER = "spark.running"
STALE_LOCK_SECONDS = 3 * 3600
_WINDOWS_REPLACE = os.name == "nt"
PROFILE_CACHE_DIRS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
    "GrShaderCache",
    "GraphiteDawnCache",
    "ShaderCache",
    "component_crx_cache",
)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user.

    On POSIX this is ``chmod 700``. On Windows the directory inherits the ACL of its
    parent (a folder under the user's profile is already private to that user).
    """
    created = [path, *(p for p in path.parents if not p.exists())]
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        for directory in created:  # intermediate folders too, not just the leaf
            try:
                os.chmod(directory, PRIVATE_DIR_MODE)
            except PermissionError:  # e.g. a bind mount owned by another uid
                log.debug("cannot chmod %s", directory)
    return path


def _replace_atomic(source: str | Path, target: Path) -> None:
    """Windows readers may briefly hold a file without permitting rename sharing."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if (
                not _WINDOWS_REPLACE
                or getattr(exc, "winerror", None) not in {5, 32, 33}
                or attempt == 5
            ):
                raise
            time.sleep(0.02 * (attempt + 1))


NEED_LOGIN_NOTE = "尚未登录，请重新扫码登录"
PROXY_CHANGED_NOTE = "代理配置与登录时不同，请重新扫码登录"


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON next to ``path`` first, then rename, so readers never see half a file."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _replace_atomic(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_private_bytes(path: Path, data: bytes) -> None:
    """Write raw bytes (login QR images) via a 0o600 temp file + rename."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _replace_atomic(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    """Load JSON; a missing file gives ``default``, a corrupt one is set aside."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeDecodeError):
        backup = path.with_name(f"{path.name}.corrupt")
        log.warning("%s is corrupt, moved to %s", path, backup.name)
        _replace_atomic(path, backup)
        return default


def mask_proxy(proxy: str | None) -> str:
    """Render a proxy URL without credentials, e.g. ``http://***@1.2.3.4:8080``."""
    if not proxy:
        return ""
    parts = urlsplit(proxy)
    auth = "***@" if parts.username else ""
    return f"{parts.scheme}://{auth}{_host_port(proxy)}"


def _host_port(proxy: str) -> str:
    netloc = urlsplit(proxy).netloc
    return netloc.rsplit("@", 1)[-1].lower()


def proxy_fingerprint(proxy: str | None) -> str:
    """Identity of the proxy route (scheme, user, host, port), ignoring the password.

    A rotated password keeps the same exit; a different user name may select a
    different exit (sticky sessions), so it is part of the identity.
    """
    if not proxy:
        return ""
    parts = urlsplit(proxy)
    identity = f"{parts.scheme.lower()}://{parts.username or ''}@{_host_port(proxy)}"
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


class DataDir:
    """Paths inside the data directory."""

    def __init__(self, root: Path) -> None:
        self.root = ensure_private_dir(root)

    def account_dir(self, account: str) -> Path:
        return ensure_private_dir(self.root / "accounts" / account)

    def profile_dir(self, account: str) -> Path:
        return ensure_private_dir(self.account_dir(account) / "profile")

    def login_file(self, account: str) -> Path:
        return self.account_dir(account) / "login.json"

    def sparks_file(self, account: str) -> Path:
        return self.account_dir(account) / "sparks.json"

    def ledger_file(self, day: date) -> Path:
        return ensure_private_dir(self.root / "ledger") / f"{day.isoformat()}.json"

    def new_run_dir(self, now: datetime) -> Path:
        base = ensure_private_dir(self.root / "runs")
        stamp = now.strftime("%Y%m%d-%H%M%S")
        path, n = base / stamp, 1
        while path.exists():
            n += 1
            path = base / f"{stamp}-{n}"
        return ensure_private_dir(path)

    @property
    def lock_file(self) -> Path:
        return self.root / RUN_MARKER

    @property
    def jobs_dir(self) -> Path:
        return ensure_private_dir(self.root / "jobs")

    @property
    def last_run_file(self) -> Path:
        return self.root / "last_run.json"


@dataclass(slots=True)
class LoginRecord:
    """Environment fingerprint captured when an account was logged in."""

    account: str
    egress_ip: str
    proxy: str
    proxy_fingerprint: str
    profile_dir: str
    host: str
    environment: str
    logged_in_at: str
    nickname: str = ""
    user_id: str = ""
    profile_updated_at: str = ""
    avatar_url: str = ""

    def save(self, path: Path) -> None:
        write_json_atomic(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> LoginRecord | None:
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                **{
                    key: data[key]
                    for key, field in cls.__dataclass_fields__.items()
                    if key in data or field.default is MISSING
                }
            )
        except KeyError:
            return None


@dataclass(slots=True)
class SparkRecord:
    """Per-friend spark-streak days as of the latest manual conversation fetch."""

    updated_at: str
    days: dict[str, int]

    def save(self, path: Path) -> None:
        write_json_atomic(path, {"updated_at": self.updated_at, "days": self.days})

    @classmethod
    def load(cls, path: Path) -> SparkRecord | None:
        data = read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            return None
        days = {
            str(name): value
            for name, value in data["days"].items()
            if type(value) is int and not isinstance(value, bool)
        }
        return cls(updated_at=str(data.get("updated_at") or ""), days=days)


def _is_unresolved(entry: dict[str, Any]) -> bool:
    if entry.get("ok") is True:
        return False
    attempts = entry.get("attempts")
    return not (
        entry.get("ok") is False
        and entry.get("outcome") in ("failed", "friend_error", "risk", "logged_out")
        and type(attempts) is int
        and attempts >= 1
    )


def _is_carried_hold(entry: dict[str, Any]) -> bool:
    return bool(entry.get("carried_from")) and _is_unresolved(entry)


class Ledger:
    """Results of the current day, keyed by account then friend."""

    def __init__(self, path: Path) -> None:
        self.path = path
        existed = path.exists()
        try:
            raw = read_json(path, {})
        except OSError:
            log.exception("cannot read ledger; sending is blocked")
            self.blocked = True
            self.entries = {}
            return
        self.blocked = (existed and not path.exists()) or not isinstance(raw, dict)
        if isinstance(raw, dict):
            self.blocked = self.blocked or bool(raw.get("!blocked"))
            self.blocked = self.blocked or any(
                not isinstance(friends, dict)
                or any(not isinstance(e, dict) for e in friends.values())
                for account, friends in raw.items()
                if account != "!blocked"
            )
        self.entries: dict[str, dict[str, dict[str, Any]]] = {
            account: {f: e for f, e in friends.items() if isinstance(e, dict)}
            for account, friends in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(friends, dict)
        }
        if self.blocked:
            try:
                self._save()
            except OSError:
                log.exception("cannot persist ledger inspection hold; sending remains blocked")
        else:
            self._inherit_unknown()

    def _inherit_unknown(self) -> None:
        """Carry unresolved sends across dates without rewriting historical ledgers."""
        try:
            today = date.fromisoformat(self.path.stem)
        except ValueError:
            return  # isolated/non-daily ledgers used by integrations and tests
        latest: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}
        carried: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}
        future_latest: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}
        future_carried: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}

        def prefer_previous(previous: dict[str, Any], candidate: dict[str, Any]) -> bool:
            try:
                before = datetime.fromisoformat(previous["at"])
                after = datetime.fromisoformat(candidate["at"])
                return (
                    before.tzinfo is not None
                    and after.tzinfo is not None
                    and (
                        before.timestamp() > after.timestamp()
                        or (
                            before.timestamp() == after.timestamp()
                            and _is_unresolved(previous)
                            and not _is_unresolved(candidate)
                        )
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                return False

        def collect(raw: dict[str, Any], day: str, *, future: bool = False) -> None:
            originals, inherited = (future_latest, future_carried) if future else (latest, carried)
            for account, friends in raw.items():
                if account == "!blocked":
                    continue
                groups: dict[str, dict[str, dict[str, Any]]] = {}
                for name, entry in friends.items():
                    groups.setdefault(normalize_name(name), {})[name] = entry
                for canonical, aliases in groups.items():
                    ordinary = {n: e for n, e in aliases.items() if not _is_carried_hold(e)}
                    source = ordinary or aliases
                    entry = self._combine_entries(source.values())
                    target = originals if ordinary else inherited
                    key = (account, canonical)
                    previous = target.get(key)
                    if future and previous and prefer_previous(previous[1], entry):
                        continue
                    target[key] = (next(iter(source)), entry, day)

        history = []
        try:
            if _corrupt_ledger_backups(self.path.parent, date.max):
                raise ValueError("historical ledger corruption backup requires inspection")
            for path in self.path.parent.glob("*.json"):
                try:
                    day = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if day != today:
                    history.append((day, path))
            for day, path in sorted(history):
                raw = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(raw, dict)
                    or bool(raw.get("!blocked"))
                    or any(
                        not isinstance(friends, dict)
                        or any(not isinstance(entry, dict) for entry in friends.values())
                        for account, friends in raw.items()
                        if account != "!blocked"
                    )
                ):
                    raise ValueError(f"invalid historical ledger: {path.name}")
                collect(raw, day.isoformat(), future=day > today)
            collect(self.entries, today.isoformat())
        except (OSError, ValueError, UnicodeError):
            log.exception("historical ledger is unreadable; sending is blocked")
            self.blocked = True
            return

        entries = {
            account: {name: entry for name, entry in friends.items() if not _is_carried_hold(entry)}
            for account, friends in self.entries.items()
        }
        for key, value in carried.items():
            latest.setdefault(key, value)
        for key, value in future_carried.items():
            future_latest.setdefault(key, value)
        for key, value in future_latest.items():
            previous = latest.get(key)
            if previous and prefer_previous(previous[1], value[1]):
                continue
            latest[key] = value
        for (account, _canonical), (name, entry, source_day) in latest.items():
            # A timezone/clock rollback can put an existing send in a later-dated
            # file. Preserve its success/hold, while failures keep today's budget.
            if _is_unresolved(entry) or (
                source_day > today.isoformat() and entry.get("ok") is True
            ):
                if source_day == today.isoformat() and not entry.get("carried_from"):
                    continue  # today's own reservation/receipt is an original, not a carry
                entries.setdefault(account, {})[name] = dict(entry) | {
                    "carried_from": entry.get("carried_from") or source_day,
                }
        self.entries = (
            entries  # reads only merge in memory; reserve/record persist under the run lock
        )

    def _save(self) -> None:
        payload = self.entries | ({"!blocked": True} if self.blocked else {})
        write_json_atomic(self.path, payload)

    def is_done(self, account: str, friend: str) -> bool:
        return any(e.get("ok") is True for e in self._aliases(account, friend).values())

    def _aliases(self, account: str, friend: str) -> dict[str, dict[str, Any]]:
        wanted = normalize_name(friend)
        return {
            key: value
            for key, value in self.entries.get(account, {}).items()
            if normalize_name(key) == wanted
        }

    def _entry(self, account: str, friend: str) -> dict[str, Any] | None:
        aliases = self._aliases(account, friend)
        if not aliases:
            return None
        return self._combine_entries(aliases.values())

    @staticmethod
    def _combine_entries(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
        values = list(entries)
        for entry in values:
            if entry.get("ok") is True:
                return dict(entry)
        failures = []
        for entry in values:
            attempts = entry.get("attempts")
            if (
                entry.get("ok") is not False
                or entry.get("outcome") not in ("failed", "friend_error")
                or type(attempts) is not int
                or attempts < 1
            ):
                return dict(entry)  # every uncertain alias retains its inspection hold
            failures.append(entry)
        return dict(failures[-1]) | {"attempts": sum(e["attempts"] for e in failures)}

    def entry(self, account: str, friend: str) -> dict[str, Any] | None:
        """Read a conversation's result across display-name spelling aliases."""
        return self._entry(account, friend)

    def _canonical_key(self, account: str, friend: str) -> str:
        return next(iter(self._aliases(account, friend)), friend)

    def _replace_entry(self, account: str, friend: str, entry: dict[str, Any]) -> None:
        key = self._canonical_key(account, friend)
        entries = self.entries.setdefault(account, {})
        for alias in self._aliases(account, friend):
            del entries[alias]
        entries[key] = entry

    def pending(self, account: str, friends: list[str]) -> list[str]:
        if self.blocked:
            return []  # ledger loss cannot authorize duplicates; retain a durable inspection hold
        pending = []
        seen = set()
        for friend in dict.fromkeys(friends):
            canonical = normalize_name(friend)
            if canonical in seen:
                continue
            seen.add(canonical)
            entry = self._entry(account, friend)
            if entry is None:
                pending.append(friend)
                continue
            if self.is_done(account, friend):
                continue
            if entry.get("ok") is not False or entry.get("outcome") not in (
                "failed",
                "friend_error",
            ):
                continue  # only a known ordinary failure can authorize an automatic retry
            attempts = entry.get("attempts")
            if type(attempts) is not int or attempts < 1:
                continue  # malformed attempt count must never grant another send
            if attempts < 3:
                pending.append(friend)
        return pending

    def held(self, account: str, friends: list[str]) -> dict[str, dict[str, Any]]:
        """Unresolved targets excluded from automatic retries, retained for reporting."""
        pending = {normalize_name(f) for f in self.pending(account, friends)}
        return {
            friend: entry
            for friend in dict.fromkeys(friends)
            if (entry := self._entry(account, friend)) is not None
            and normalize_name(friend) not in pending
            and not self.is_done(account, friend)
        }

    def reserve(self, account: str, friend: str, when: datetime) -> None:
        """Persist intent BEFORE send; a crash leaves this friend held for inspection."""
        if friend not in self.pending(account, [friend]):
            raise ValueError("该好友已完成、待核对或超过当天尝试上限")
        previous = self._entry(account, friend) or {}
        owner = read_json(self.path.parent.parent / RUN_MARKER / "owner.json", {})
        identity = (
            owner.get("identity")
            if isinstance(owner, dict)
            and owner.get("pid") == os.getpid()
            and owner.get("host") == socket.gethostname()
            else None
        )
        active_run = {"run_identity": identity} if isinstance(identity, str) and identity else {}
        self._replace_entry(
            account,
            friend,
            {
                "ok": False,
                "outcome": "sending",
                "reason": "正在等待平台发送回执",
                "message_id": None,
                "at": when.isoformat(timespec="seconds"),
                "attempts": int(previous.get("attempts", 0)) + 1,
                **active_run,
            },
        )
        self._save()

    def record(self, account: str, friend: str, receipt: Receipt, when: datetime) -> None:
        previous = self._entry(account, friend) or {}
        self._replace_entry(
            account,
            friend,
            {
                "ok": receipt.ok,
                "outcome": receipt.outcome.value,
                "reason": receipt.reason,
                "message_id": receipt.message_id,
                "at": when.isoformat(timespec="seconds"),
                "attempts": int(previous.get("attempts", 0))
                + (previous.get("outcome") != "sending"),
            },
        )
        self._save()

    def failures(self, account: str) -> dict[str, str]:
        return {
            friend: entry.get("reason", "")
            for friend, entry in self.entries.get(account, {}).items()
            if not entry.get("ok")
        }


class RunBusyError(RuntimeError):
    """An earlier trigger is still running."""


def _corrupt_ledger_backups(directory: Path, today: date) -> list[Path]:
    backups = []
    for path in directory.glob("*.json.corrupt*"):
        try:
            day = date.fromisoformat(path.name.partition(".json.corrupt")[0])
        except ValueError:
            continue
        if day <= today:
            backups.append(path)
    return backups


def _review_evidence_ledgers(directory: Path, today: date) -> set[Path]:
    """Retain unknown originals and the latest records that resolve them."""
    dated = []
    for path in directory.glob("*.json"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day <= today:
            dated.append((day, path))
    all_paths = {path for _day, path in dated}
    if _corrupt_ledger_backups(directory, today):
        return all_paths
    protected: set[Path] = set()
    uncertain: set[tuple[str, str]] = set()
    latest: dict[tuple[str, str], Path] = {}
    try:
        for _day, path in sorted(dated):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or bool(raw.get("!blocked")):
                return all_paths
            for account, friends in raw.items():
                if account == "!blocked":
                    continue
                if not isinstance(friends, dict):
                    return all_paths
                for name, entry in friends.items():
                    if not isinstance(entry, dict):
                        return all_paths
                    key = (account, normalize_name(name))
                    if not _is_carried_hold(entry):
                        latest[key] = path
                    if _is_unresolved(entry) or (
                        entry.get("ok") is not True
                        and entry.get("outcome") in ("risk", "logged_out")
                    ):
                        uncertain.add(key)
                        protected.add(path)
    except (OSError, ValueError, UnicodeError):
        return all_paths
    protected.update(latest[key] for key in uncertain if key in latest)
    return protected


def pid_alive(pid: int) -> bool:
    """Best-effort check whether a local process id is still running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _marker_is_stale(marker: Path) -> tuple[bool, str]:
    age = time.time() - marker.stat().st_mtime
    owner = read_json(marker / "owner.json", {})
    pid = owner.get("pid") if isinstance(owner, dict) else None
    same_host = isinstance(owner, dict) and owner.get("host") == socket.gethostname()
    known_local_process = same_host and isinstance(pid, int)
    if known_local_process and not pid_alive(pid):
        return True, f"原进程 pid {pid} 已退出"
    if not known_local_process and age > STALE_LOCK_SECONDS:
        return True, f"标记已过期（{age:.0f} 秒）"
    detail = f"已运行 {age:.0f} 秒"
    if pid:
        detail += f"（pid {pid}）"
    return False, detail


def marker_owner(marker: Path) -> dict[str, Any] | None:
    """Who holds the run marker, or ``None`` when it is free (or left by a crash)."""
    if not marker.is_dir():
        return None
    try:
        stale, why = _marker_is_stale(marker)
    except OSError:
        return None
    if stale:
        return None
    owner = read_json(marker / "owner.json", {})
    return {"pid": owner.get("pid") if isinstance(owner, dict) else None, "detail": why}


@contextmanager
def exclusive_run(marker: Path) -> Iterator[None]:
    """Hold an OS lock before inspecting/reclaiming the compatible owner directory."""
    identity = secrets.token_hex(16)
    try:
        with file_lock(marker.with_name(f".{marker.name}.lock")):
            if marker.exists():
                if not marker.is_dir():
                    raise RunBusyError("运行标记格式异常，请人工检查")
                stale, why = _marker_is_stale(marker)
                if not stale:
                    raise RunBusyError(f"另一个 dyspark 进程正在运行（{why}）")
                log.warning("removing stale run marker: %s", why)
                shutil.rmtree(marker)
            marker.mkdir()
            try:
                write_json_atomic(
                    marker / "owner.json",
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "since": time.time(),
                        "identity": identity,
                    },
                )
                yield
            finally:
                owner = read_json(marker / "owner.json", {})
                if isinstance(owner, dict) and owner.get("identity") == identity:
                    shutil.rmtree(marker, ignore_errors=True)
    except LockBusyError:
        raise RunBusyError("另一个 dyspark 进程正在运行（独占锁正在使用）") from None


def purge_old_files(data: DataDir, today: date, keep_days: int) -> list[Path]:
    """Expire artefacts and resolved ledgers; preserve inspection evidence."""
    cutoff = today - timedelta(days=keep_days)
    removed: list[Path] = []
    for run in (data.root / "runs").glob("*"):
        try:
            run_day = datetime.strptime(run.name[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if run_day < cutoff:
            shutil.rmtree(run, ignore_errors=True)
            removed.append(run)
    for job in (data.root / "jobs").glob("*-*"):
        try:
            job_day = datetime.strptime(job.name.split("-")[1], "%Y%m%d").date()
        except (IndexError, ValueError):
            continue
        if job_day < cutoff:
            shutil.rmtree(job, ignore_errors=True)
            removed.append(job)
    ledger_dir = data.root / "ledger"
    evidence = _review_evidence_ledgers(ledger_dir, today)
    for ledger in ledger_dir.glob("*.json"):
        try:
            ledger_day = date.fromisoformat(ledger.stem)
        except ValueError:
            continue
        if ledger_day < cutoff:
            if ledger in evidence:
                continue
            ledger.unlink(missing_ok=True)
            removed.append(ledger)
    return removed


def trim_profile_cache(profile: Path) -> int:
    """Remove disposable cache folders from a closed browser profile; return bytes freed."""
    freed = 0
    for relative in PROFILE_CACHE_DIRS:
        target = profile / relative
        if not target.is_dir():
            continue
        for item in target.rglob("*"):
            try:
                freed += item.stat().st_size if item.is_file() else 0
            except OSError:
                continue
        shutil.rmtree(target, ignore_errors=True)
    return freed
