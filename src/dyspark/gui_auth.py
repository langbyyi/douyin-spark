"""Single-password credentials and bounded, revocable panel sessions.

No HTTP or business logic here. Only password hashes persist; sessions and rate
limits belong to one GUI process and are discarded when it stops.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dyspark.locking import LockBusyError, file_lock
from dyspark.store import ensure_private_dir

PASSWORD_FILE = "gui-auth.json"
PASSWORD_EDIT_LOCK = ".gui-auth.edit.lock"
PASSWORD_SERVICE_LOCK = ".gui-auth.service.lock"
CREDENTIAL_VERSION = 2
COOKIE_NAME = "spark_session"
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 3600
IDLE_SECONDS = 30 * 60
ATTEMPT_WINDOW = 60
MAX_PEER_ATTEMPTS = 5
MAX_TOTAL_ATTEMPTS = 20
MAX_PEERS = 256
MAX_SESSIONS = 32


class AuthError(ValueError):
    """Unsafe or unreadable GUI authentication configuration."""


def password_bytes(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise AuthError("面板密码必须是有效的 UTF-8 文字") from exc
    if not encoded or len(encoded) > 1024:
        raise AuthError("面板密码必须为 1–1024 个 UTF-8 字节")
    return encoded


@dataclass(frozen=True)
class PasswordHash:
    salt: bytes
    digest: bytes

    @classmethod
    def create(cls, password: str) -> PasswordHash:
        encoded = password_bytes(password)
        if len(password) < 8 or not password.strip():
            raise AuthError("面板密码至少需要 8 个字符，且不能全为空白")
        salt = secrets.token_bytes(16)
        return cls(salt, hashlib.pbkdf2_hmac("sha256", encoded, salt, ITERATIONS))

    def verify(self, password: str) -> bool:
        digest = hashlib.pbkdf2_hmac("sha256", password_bytes(password), self.salt, ITERATIONS)
        return secrets.compare_digest(digest, self.digest)

    def record(self) -> dict:
        return {
            "version": 1,
            "iterations": ITERATIONS,
            "salt": self.salt.hex(),
            "digest": self.digest.hex(),
        }

    @classmethod
    def read(cls, path: Path) -> PasswordHash:
        return _read_password_record(path)[0]

    @classmethod
    def from_record(cls, doc: dict) -> PasswordHash:
        if not isinstance(doc, dict) or doc.get("version") != 1:
            raise ValueError("invalid credential version")
        if doc.get("iterations") != ITERATIONS:
            raise ValueError("invalid work factor")
        salt, digest = bytes.fromhex(doc["salt"]), bytes.fromhex(doc["digest"])
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError("invalid hash")
        return cls(salt, digest)


def _read_password_record(path: Path) -> tuple[PasswordHash, dict]:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError("invalid credential file")
        doc = json.loads(path.read_text(encoding="utf-8"))
        hashed = PasswordHash.from_record(doc)
        if doc.get("credential_version") not in (None, CREDENTIAL_VERSION):
            raise ValueError("unsupported credential version")
        _protect_file(path)
        return hashed, doc
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise AuthError(
            f"面板认证文件无法读取或保护：{path}。请停止面板服务后执行 spark password reset；"
            "不会自动覆盖原文件"
        ) from exc


def _protect_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    _protect_windows_file(path)


def _protect_windows_file(path: Path) -> None:
    # Replace the entire protected DACL, including existing explicit grants.
    # chmod and icacls /grant:r cannot provide that guarantee on Windows.
    from ctypes import wintypes

    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        sid = next(csv.reader(io.StringIO(result.stdout)))[1]
    except (IndexError, StopIteration) as exc:
        raise OSError("cannot identify current Windows account") from exc
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise OSError("cannot identify current Windows account")

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    convert.restype = wintypes.BOOL
    get_dacl = api.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    set_security = api.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert(f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise OSError("missing protected Windows DACL")
        # SE_FILE_OBJECT=1; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        result_code = set_security(str(path), 1, 0x80000004, None, None, dacl, None)
        if result_code:
            raise ctypes.WinError(result_code)
    finally:
        kernel.LocalFree(descriptor)


def load_password(data_dir: Path, env: Mapping[str, str]) -> tuple[PasswordHash, str | None]:
    """Initialize once from the environment or randomness, then use the stored hash."""
    ensure_private_dir(data_dir)
    try:
        with file_lock(data_dir / PASSWORD_EDIT_LOCK, timeout=5):
            return _load_password(data_dir, env)
    except LockBusyError as exc:
        raise AuthError("面板认证文件正在修改，请稍后重试") from exc


def _load_password(data_dir: Path, env: Mapping[str, str]) -> tuple[PasswordHash, str | None]:
    configured = env.get("SPARK_GUI_PASSWORD", "")
    path = data_dir / PASSWORD_FILE
    if path.exists() or path.is_symlink():
        hashed, doc = _read_password_record(path)
        if doc.get("credential_version") == CREDENTIAL_VERSION:
            return hashed, None
        # Preserve the old effective password once, then discard its env precedence rules.
        if configured:
            deployed = PasswordHash.create(configured)
            baseline = doc.get("environment") if doc.get("managed_by_panel") is True else None
            try:
                use_panel = baseline is not None and PasswordHash.from_record(baseline).verify(
                    configured
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise AuthError(
                    "旧版面板密码变更记录损坏，请停止服务后执行 spark password reset"
                ) from exc
            if not use_panel:
                hashed = deployed
        _write_password(path, hashed)
        return hashed, None
    password = configured or secrets.token_urlsafe(24)
    hashed = PasswordHash.create(password)
    if not _write_password(path, hashed, exclusive=True):
        return _read_password_record(path)[0], None
    return hashed, None if configured else password


def save_password(path: Path, hashed: PasswordHash) -> None:
    """Publish only protected hashes; failure leaves the previous credential intact."""
    ensure_private_dir(path.parent)
    try:
        with file_lock(path.parent / PASSWORD_EDIT_LOCK, timeout=5):
            _write_password(path, hashed)
    except LockBusyError as exc:
        raise AuthError("面板认证文件正在修改，请稍后重试") from exc


def _write_password(path: Path, hashed: PasswordHash, *, exclusive: bool = False) -> bool:
    if path.is_symlink():
        raise AuthError("认证文件不能是符号链接")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".gui-auth-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            record = {**hashed.record(), "credential_version": CREDENTIAL_VERSION}
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _protect_file(temporary)
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        else:
            os.replace(temporary, path)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthError("无法保存面板密码，请检查数据目录权限或磁盘空间") from exc
    finally:
        temporary.unlink(missing_ok=True)


class LoginLimited(ValueError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("登录尝试过于频繁，请稍后重试")


@dataclass
class _Session:
    expires: float
    touched: float


class PanelAuth:
    def __init__(self, password: PasswordHash, *, clock: Callable[[], float] = time.monotonic):
        self.password = password
        self.clock = clock
        self._sessions: dict[str, _Session] = {}
        self._peers: dict[str, deque[float]] = {}
        self._attempts: deque[float] = deque()
        self._lock = threading.Lock()

    def _attempt(self, peer: str, now: float) -> None:
        attempts = self._peers.get(peer, deque())
        limited = (
            self._attempts
            if len(self._attempts) >= MAX_TOTAL_ATTEMPTS
            else attempts
            if len(attempts) >= MAX_PEER_ATTEMPTS
            else None
        )
        if limited is not None:
            raise LoginLimited(max(1, math.ceil(ATTEMPT_WINDOW - (now - limited[0]))))
        if peer not in self._peers and len(self._peers) >= MAX_PEERS:
            raise LoginLimited(ATTEMPT_WINDOW)
        self._peers[peer] = attempts
        self._attempts.append(now)
        attempts.append(now)

    def change_password(
        self,
        current: str,
        new: str,
        session: str | None,
        peer: str,
        path: Path,
    ) -> bool:
        password_bytes(current)
        password_bytes(new)
        with self._lock:
            now = self.clock()
            self._prune(now)
            if (
                not session
                or len(session) > 128
                or not session.isascii()
                or self._key(session) not in self._sessions
            ):
                raise AuthError("面板会话已过期，请重新登录")
            self._attempt(peer, now)
            if not self.password.verify(current):
                return False
            if current == new:
                raise AuthError("新密码不能与当前密码相同")
            hashed = PasswordHash.create(new)
            save_password(path, hashed)
            self.password = hashed
            self._sessions.clear()
            return True

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        self._sessions = {
            key: session
            for key, session in self._sessions.items()
            if now < session.expires and now - session.touched < IDLE_SECONDS
        }
        while self._attempts and now - self._attempts[0] >= ATTEMPT_WINDOW:
            self._attempts.popleft()
        for peer, attempts in list(self._peers.items()):
            while attempts and now - attempts[0] >= ATTEMPT_WINDOW:
                attempts.popleft()
            if not attempts:
                del self._peers[peer]

    def login(self, password: str, peer: str, previous: str | None = None) -> str | None:
        password_bytes(password)
        # Serialise hashing too: a login flood cannot schedule unbounded CPU work.
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._attempt(peer, now)
            if not self.password.verify(password):
                return None
            if previous and len(previous) <= 128 and previous.isascii():
                self._sessions.pop(self._key(previous), None)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].touched)
                del self._sessions[oldest]
            token = secrets.token_urlsafe(32)
            self._sessions[self._key(token)] = _Session(now + SESSION_SECONDS, now)
            return token

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 128 or not token.isascii():
            return False
        with self._lock:
            now = self.clock()
            self._prune(now)
            session = self._sessions.get(self._key(token))
            if session is None:
                return False
            session.touched = now
            return True

    def logout(self, token: str | None) -> None:
        if token and len(token) <= 128 and token.isascii():
            with self._lock:
                self._sessions.pop(self._key(token), None)
