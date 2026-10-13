"""Remote login through a one-time web page (a relay: Douyin offers no OAuth).

The browser runs on the run machine with the account's own profile and
proxy, so the egress IP recorded at the end is the one later runs will see. A
small HTTP server publishes ``/login/<token>`` for the length of the session:

* the live QR code, re-captured from the Douyin page and refreshed when it expires;
* the login status (scanned, SMS check required, verification required, ...);
* a phone + SMS code form whose input is replayed into Douyin's own SMS login.

Threads: Playwright stays on the calling thread (:class:`WebLoginDriver`). The HTTP
server answers from its own threads and only reads :class:`RelayState` and puts
:class:`Command` objects on a queue, which the driver drains between polls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dyspark import httpd, termqr
from dyspark import selectors as sel
from dyspark.browser import open_context
from dyspark.chat import ChatPage, PageState
from dyspark.config import Account, LoginOptions, Settings
from dyspark.login import (
    LoginError,
    LoginPage,
    LoginPhase,
    complete_login,
    initial_login_ip,
    mask_phone,
    novnc_url,
    vnc_bridge,
)
from dyspark.login_transport import IMAGE_ROUTES, WRITE_ROUTES
from dyspark.notify import Notifier
from dyspark.relay_page import page_policy, render_page
from dyspark.store import (
    LoginRecord,
    ensure_private_dir,
    mask_proxy,
    write_json_atomic,
    write_private_bytes,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

SESSION_FILE = "session.json"
CANCEL_FILE = "cancel"
POLL_SECONDS = 1.0
VERIFY_RESULT_SECONDS = 15.0
LINGER_SECONDS = 6.0  # keep answering briefly so an open page can show the outcome
MAX_BODY = 4096
_PHONE = re.compile(r"^1\d{10}$")
_CODE = re.compile(r"^\d{4,8}$")


class Status(StrEnum):
    STARTING = "starting"
    PENDING = "pending"  # QR on screen; waiting for a scan or the SMS form
    SCANNED = "scanned"  # waiting for the confirmation tap on the phone
    SMS_REQUIRED = "sms_required"  # an SMS code is needed (SMS tab or post-scan check)
    VERIFICATION_REQUIRED = "verification_required"  # captcha: needs a human on the page
    FINALIZING = "finalizing"  # platform login complete; checking and saving the environment
    SUCCESS = "success"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (Status.SUCCESS, Status.EXPIRED, Status.FAILED, Status.CANCELLED)


class LoginCancelled(LoginError):
    pass


class LoginExpired(LoginError):
    pass


# ----------------------------------------------------------------- input helpers
def normalize_phone(raw: Any) -> str | None:
    """Mainland mobile number without separators or country code, else ``None``."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[\s\-()]", "", raw)
    for prefix in ("+86", "0086"):
        if digits.startswith(prefix):
            digits = digits[len(prefix) :]
    return digits if _PHONE.match(digits) else None


def normalize_code(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    code = raw.strip()
    return code if _CODE.match(code) else None


@dataclass(frozen=True, slots=True)
class Command:
    """A request from the web page, executed by the browser thread."""

    kind: str  # sms_send | sms_verify | use_qr | qr_refresh | cancel
    value: str | None = field(default=None, repr=False)  # phone / code: never printed


class SmsGuard:
    """Per-session limits on code requests and code submissions."""

    def __init__(
        self,
        *,
        max_sends: int = 3,
        send_interval: float = 60,
        max_verifies: int = 5,
        verify_interval: float = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_sends, self.send_interval = max_sends, send_interval
        self.max_verifies, self.verify_interval = max_verifies, verify_interval
        self.clock = clock
        self.sends = self.verifies = 0
        self._last_send: float | None = None
        self._last_verify: float | None = None
        self._lock = threading.Lock()

    def take_send(self) -> str | None:
        """Reserve a code request; returns a refusal message instead when over the limit."""
        with self._lock:
            now = self.clock()
            if self.sends >= self.max_sends:
                return f"本次登录最多获取 {self.max_sends} 次验证码，已达上限"
            if self._last_send is not None and now - self._last_send < self.send_interval:
                wait = self.send_interval - (now - self._last_send)
                return f"请 {wait:.0f} 秒后再获取验证码"
            self.sends += 1
            self._last_send = now
            return None

    def take_verify(self) -> str | None:
        with self._lock:
            now = self.clock()
            if self.verifies >= self.max_verifies:
                return f"验证码最多提交 {self.max_verifies} 次，已达上限，请重新发起登录"
            if self._last_verify is not None and now - self._last_verify < self.verify_interval:
                return "提交太频繁，请稍候再试"
            self.verifies += 1
            self._last_verify = now
            return None

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            now = self.clock()
            wait = 0.0
            if self._last_send is not None:
                wait = max(0.0, self.send_interval - (now - self._last_send))
            return {
                "sends_left": self.max_sends - self.sends,
                "verifies_left": self.max_verifies - self.verifies,
                "resend_in": math.ceil(wait),
            }


# ----------------------------------------------------------------------- state
class RelayState:
    """What the page and the MCP tools can see; written by the browser thread."""

    def __init__(
        self, session_id: str, account: str, expires_at: datetime, *, proxy: str | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._listeners: list[Callable[[RelayState], None]] = []
        self.session_id = session_id
        self.account = account
        self.expires_at = expires_at
        self.started_at = datetime.now(expires_at.tzinfo)
        self.stage = "browser"
        self.login_source: str | None = None
        self.proxy = proxy  # masked: the route this login's browser was launched with
        self.status = Status.STARTING
        self.message = "正在打开抖音登录页…"
        self.url: str | None = None
        self.phone_input = True
        self.sms_tab = False
        self.secondary_verification = False
        self._bound_sms_sent = False
        self.phone_hint: str | None = None
        self.sms_resend_in = 0
        self.sms_action: dict[str, str] | None = None
        self.sms_diagnostics: dict | None = None
        self.phone: str | None = None  # masked
        self.novnc_url: str | None = None
        self.egress_ip: str | None = None
        self.qr_png: bytes | None = None
        self.qr_version = 0
        self.qr_refreshing = False
        self.screen_png: bytes | None = None
        self.screen_version = 0
        self._digests = {"qr": "", "screen": ""}

    def subscribe(self, listener: Callable[[RelayState], None]) -> None:
        self._listeners.append(listener)

    def update(self, **changes: Any) -> None:
        with self._lock:
            changed = False
            for name, value in changes.items():
                if not hasattr(self, name) or name.startswith("_"):
                    raise AttributeError(name)
                if getattr(self, name) != value:
                    setattr(self, name, value)
                    changed = True
            bound_sent = self._bound_sms_sent
            if not self.secondary_verification:
                bound_sent = False
            elif self.status is Status.SMS_REQUIRED and (
                self.message.startswith("短信已发送至")
                or (
                    not self.phone_input
                    and any(text in self.message for text in sel.SMS_SENT_TEXTS)
                )
            ):
                bound_sent = True
            if self._bound_sms_sent != bound_sent:
                self._bound_sms_sent = bound_sent
                changed = True
            if bound_sent and self.phone_input:
                self.phone_input = False
                changed = True
        if changed:
            self._notify()

    def set_image(self, kind: str, png: bytes) -> bool:
        """Store a new QR (``kind="qr"``) or page screenshot; ``True`` if it changed."""
        digest = hashlib.sha256(png).hexdigest()
        with self._lock:
            if digest == self._digests[kind]:
                return False
            self._digests[kind] = digest
            setattr(self, f"{kind}_png", png)
            setattr(self, f"{kind}_version", getattr(self, f"{kind}_version") + 1)
        self._notify()
        return True

    def image(self, kind: str) -> bytes | None:
        with self._lock:
            return getattr(self, f"{kind}_png")

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            now = now or datetime.now(self.expires_at.tzinfo)
            return {
                "session_id": self.session_id,
                "account": self.account,
                "proxy": self.proxy,
                "status": self.status.value,
                "finished": self.status.finished,
                "message": self.message,
                "url": self.url,
                "expires_at": self.expires_at.isoformat(timespec="seconds"),
                "started_at": self.started_at.isoformat(),
                "elapsed_seconds": max(0, int((now - self.started_at).total_seconds())),
                "stage": self.stage,
                "login_source": self.login_source,
                "expires_in": max(0, int((self.expires_at - now).total_seconds())),
                "qr_version": self.qr_version,
                "qr_refreshing": self.qr_refreshing,
                "screen_version": self.screen_version,
                "phone_input": self.phone_input,
                "secondary_verification": self.secondary_verification,
                "phone_hint": self.phone_hint,
                "sms_resend_in": self.sms_resend_in,
                "sms_sent": self._bound_sms_sent,
                "sms_action": dict(self.sms_action) if self.sms_action else None,
                "sms_diagnostics": self.sms_diagnostics,
                "sms_tab": self.sms_tab,
                "phone": self.phone,
                "novnc_url": self.novnc_url,
                "egress_ip": self.egress_ip,
                "updated_at": now.isoformat(timespec="seconds"),
            }

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self)
            except Exception:  # a failing observer must not break the login
                log.debug("relay listener failed", exc_info=True)


class SessionFiles:
    """Mirror the relay state into a private directory (read by the MCP tools)."""

    def __init__(self, directory: Path) -> None:
        self.directory = ensure_private_dir(directory)
        self._written = {"qr": 0, "screen": 0}

    def __call__(self, state: RelayState) -> None:
        for kind in ("qr", "screen"):
            version = getattr(state, f"{kind}_version")
            png = state.image(kind)
            if png and version != self._written[kind]:
                write_private_bytes(self.directory / f"{kind}.png", png)
                self._written[kind] = version
        snapshot = state.snapshot()
        snapshot["qr_path"] = str(self.directory / "qr.png") if state.qr_version else None
        snapshot["screen_path"] = (
            str(self.directory / "screen.png") if state.screen_version else None
        )
        write_json_atomic(self.directory / SESSION_FILE, snapshot)

    def cancel_requested(self) -> bool:
        return (self.directory / CANCEL_FILE).exists()


# ---------------------------------------------------------------------- server
@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _json(status: int, payload: dict[str, Any]) -> Reply:
    return Reply(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


NOT_FOUND = Reply(404, b"Not Found", "text/plain; charset=utf-8")


class RelayServer:
    """Routes for ``/login/<token>``; anything else is a plain 404."""

    def __init__(
        self,
        state: RelayState,
        commands: queue.Queue[Command],
        options: LoginOptions,
        *,
        token: str | None = None,
        guard: SmsGuard | None = None,
    ) -> None:
        self.state = state
        self.commands = commands
        self.options = options
        self.token = token or secrets.token_urlsafe(24)
        self.guard = guard or SmsGuard()
        self._operation_lock = threading.Lock()
        self.cancelled = threading.Event()
        self._qr_refresh_at: float | None = None
        # The POST handlers, checked against login_transport's client whitelist so a
        # new route cannot silently lose its panel/MCP proxy (drift fails at startup).
        self._routes: dict[tuple[str, str], Callable[[dict[str, Any]], Reply]] = {
            ("POST", "sms/send"): self._sms_send,
            ("POST", "sms/verify"): self._sms_verify,
            ("POST", "qr/refresh"): self._qr_refresh,
            ("POST", "use-qr"): lambda _payload: self._enqueue(
                Command("use_qr"), "正在切换到扫码登录…"
            ),
            ("POST", "cancel"): lambda _payload: self._enqueue(Command("cancel"), "正在取消…"),
        }
        unknown = set(self._routes) - {("POST", tail) for tail in WRITE_ROUTES}
        if unknown:
            raise RuntimeError(f"login routes missing from login_transport whitelist: {unknown}")

    @property
    def url(self) -> str:
        if self.options.public_url:
            base = self.options.public_url.rstrip("/")
        else:
            bind = self.options.bind
            host = httpd.lan_address() if bind in ("0.0.0.0", "::") else bind
            host = f"[{host}]" if ":" in host else host
            base = f"http://{host}:{self.options.port}"
        return f"{base}/login/{self.token}"

    @contextmanager
    def running(self) -> Iterator[None]:
        try:
            server = httpd.bind_server(self.options.bind, self.options.port, self._handler())
        except OSError as exc:
            raise LoginError(
                f"无法在 {self.options.bind}:{self.options.port} 上开启登录页（{exc}），"
                "请检查端口是否被占用，或修改 login.port"
            ) from exc
        if self.options.port == 0:
            self.options = replace(self.options, port=server.server_address[1])
        log.info("login relay listening on %s:%d", self.options.bind, self.options.port)
        with httpd.serving(server):
            yield

    # -------------------------------------------------------------- routing
    def respond(self, method: str, raw_path: str, content_type: str, body: bytes) -> Reply:
        path = urlsplit(raw_path).path
        prefix, _, rest = path.partition("/login/")
        if prefix or not rest:
            return NOT_FOUND
        token, _, tail = rest.partition("/")
        if not secrets.compare_digest(token.encode(), self.token.encode()):
            return NOT_FOUND
        if method == "GET":
            return self._get(tail)
        if (method, tail) not in self._routes:
            return NOT_FOUND
        if not content_type.lower().startswith("application/json"):
            return _json(415, {"message": "请求格式不支持"})
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return _json(400, {"message": "请求内容无效"})
        if not isinstance(payload, dict):
            return _json(400, {"message": "请求内容无效"})
        with self._operation_lock:
            return self._post(tail, payload)

    def _post(self, tail: str, payload: dict[str, Any]) -> Reply:
        if self.state.status.finished:
            return _json(409, {"message": "本次登录已结束"})
        if self.state.status is Status.FINALIZING:
            return _json(409, {"message": "登录验证已完成，正在保存账号，请等待保存结束"})
        current = self.state.snapshot()
        action = current["sms_action"] or {}
        if tail in ("sms/send", "sms/verify") and action.get("status") in (
            "queued",
            "running",
            "waiting",
        ):
            return _json(409, {"message": "上一项验证操作仍在处理，请等待页面反馈，不要重复提交"})
        if tail == "sms/send" and current["status"] not in (Status.PENDING, Status.SMS_REQUIRED):
            return _json(409, {"message": "当前阶段不接受短信请求，请按页面提示完成验证"})
        if tail == "sms/verify" and current["status"] != Status.SMS_REQUIRED:
            return _json(409, {"message": "当前页面未要求短信验证码"})
        if tail == "use-qr" and current["secondary_verification"]:
            return _json(409, {"message": "扫码后的二次验证需要先完成，不能切换登录方式"})
        if tail == "qr/refresh" and (
            current["status"] != Status.PENDING or current["secondary_verification"]
        ):
            return _json(409, {"message": "当前阶段不能刷新二维码，请按页面提示完成验证"})
        return self._routes["POST", tail](payload)

    def _get(self, tail: str) -> Reply:
        if tail == "":
            nonce = secrets.token_urlsafe(16)
            page = render_page(self.state.account, nonce).encode("utf-8")
            return Reply(
                200,
                page,
                "text/html; charset=utf-8",
                {"Content-Security-Policy": page_policy(nonce)},
            )
        if tail == "state":
            snapshot = self.state.snapshot()
            snapshot.pop("url", None)  # the page already knows where it is
            snapshot["sms"] = self.guard.snapshot()
            snapshot["sms"]["resend_in"] = max(
                snapshot["sms"]["resend_in"], snapshot["sms_resend_in"]
            )
            return _json(200, snapshot)
        if tail in IMAGE_ROUTES:
            png = self.state.image(tail.removesuffix(".png"))
            return Reply(200, png, "image/png") if png else NOT_FOUND
        return NOT_FOUND

    def _sms_send(self, payload: dict[str, Any]) -> Reply:
        current = self.state.snapshot()
        resend_in = current["sms_resend_in"]
        if resend_in > 0:
            return _json(429, {"message": f"短信已发送，请等待 {resend_in} 秒后重试"})
        phone = None
        if current["phone_input"]:
            phone = normalize_phone(payload.get("phone"))
            if phone is None:
                return _json(400, {"message": "请输入正确的 11 位手机号"})
        refusal = self.guard.take_send()
        if refusal:
            return _json(429, {"message": refusal})
        log.info("login page: SMS code requested%s", f" for {mask_phone(phone)}" if phone else "")
        return self._enqueue(Command("sms_send", phone), "正在请求验证码…")

    def _qr_refresh(self, _payload: dict[str, Any]) -> Reply:
        now = self.guard.clock()
        if self.state.qr_refreshing:
            return _json(409, {"message": "二维码正在刷新，请稍候"})
        if self._qr_refresh_at is not None and now - self._qr_refresh_at < 5:
            return _json(429, {"message": "刷新太频繁，请稍候再试"})
        self._qr_refresh_at = now
        self.state.update(qr_refreshing=True, message="正在刷新抖音二维码…")
        return self._enqueue(Command("qr_refresh"), "二维码刷新请求已接收")

    def _sms_verify(self, payload: dict[str, Any]) -> Reply:
        code = normalize_code(payload.get("code"))
        if code is None:
            return _json(400, {"message": "验证码应为 4~8 位数字"})
        refusal = self.guard.take_verify()
        if refusal:
            return _json(429, {"message": refusal})
        log.info("login page: SMS verification requested")
        return self._enqueue(Command("sms_verify", code), "验证码提交请求已接收，正在操作抖音页面…")

    def _enqueue(self, command: Command, message: str) -> Reply:
        if command.kind == "cancel":
            self.cancelled.set()
        if command.kind in ("sms_send", "sms_verify"):
            self.state.update(
                sms_action={"kind": command.kind, "status": "queued", "message": message}
            )
        self.commands.put(command)
        return _json(202, {"ok": True, "message": message})

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        relay = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "dyspark"
            sys_version = ""
            timeout = 15  # drop idle / trickling clients

            def do_GET(self) -> None:
                self._answer(relay.respond("GET", self.path, "", b""))

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    self._answer(_json(413, {"message": "请求过大"}))
                    return
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                self._answer(relay.respond("POST", self.path, content_type, body))

            def _answer(self, reply: Reply) -> None:
                self.send_response(reply.status)
                for name, value in {**SECURITY_HEADERS, **reply.headers}.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            def log_message(self, format: str, *args: Any) -> None:
                log.debug("relay: %s", (format % args).replace(relay.token, "<token>"))

        return Handler


# ---------------------------------------------------------------------- driver
class WebLoginDriver:
    """Watches the Douyin login page and replays the web page's commands into it."""

    def __init__(
        self,
        settings: Settings,
        state: RelayState,
        commands: queue.Queue[Command],
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
        cancel_requested: Callable[[], bool] = lambda: False,
        poll_s: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.state = state
        self.commands = commands
        self.deadline = deadline
        self.clock = clock
        self.cancel_requested = cancel_requested
        self.poll_ms = int(poll_s * 1000)
        self._stack = ExitStack()
        self._vnc_tried = False
        self._notice: str | None = None  # visible until the next user action
        self._verify_started_at: float | None = None
        self._qr_auto_retry_at = 0.0

    def drive(self, page: Page) -> ChatPage:
        """Return once the chat page is ready; raise on cancel / expiry / browser loss."""
        if self.cancel_requested():
            raise LoginCancelled("登录已取消")
        chat, login = ChatPage(page), LoginPage(page)
        self.state.update(stage="page", message="正在打开抖音登录页…")
        # Poll actual login readiness while slow deferred resources are downloading.
        chat.open(wait_for_dom=False)
        qr_prepared = False
        self.state.update(status=Status.PENDING, stage="loading", message="正在加载登录组件…")
        while True:
            if self.cancel_requested():
                raise LoginCancelled("登录已取消")
            if self.clock() >= self.deadline:
                raise LoginExpired("登录会话已过期")
            self._run_commands(page, login)
            if self._tolerant(page, lambda: chat.state(wait_s=0)) is PageState.READY:
                reused = (
                    self.state.qr_version == 0
                    and self.state.sms_action is None
                    and self.state.status in (Status.STARTING, Status.PENDING)
                )
                self.state.update(login_source="saved_session" if reused else "interactive")
                return chat
            if not qr_prepared:
                qr_prepared = self._prepare_qr(page, login)
            self._observe(page, login)
            page.wait_for_timeout(self.poll_ms)

    def close(self) -> None:
        self._stack.close()

    # ----------------------------------------------------------- internals
    def _prepare_qr(self, page: Page, login: LoginPage) -> bool:
        """Retry late login controls until QR is selected or another flow is active."""
        if self.state.sms_tab:
            return True
        ready = self._tolerant(page, lambda: page.evaluate("document.readyState"))
        if ready not in ("interactive", "complete"):
            return False
        secondary = self._tolerant(page, login.secondary_sms_visible)
        if secondary is None:
            return False
        if secondary:
            return True
        phase = self._tolerant(page, login.phase)
        if phase in (LoginPhase.QR, LoginPhase.SCANNED, LoginPhase.CAPTCHA):
            return True
        if phase is LoginPhase.UNKNOWN:
            self._tolerant(page, login.open_panel)
        # A dialog can appear during open_panel's wait; don't replace verification.
        if self._tolerant(page, login.secondary_sms_visible) is not False:
            return False
        phase = self._tolerant(page, login.phase)
        if phase in (LoginPhase.QR, LoginPhase.SCANNED, LoginPhase.CAPTCHA):
            return True
        return bool(self._tolerant(page, login.use_qr_tab))

    def _observe(self, page: Page, login: LoginPage) -> None:
        phase = self._tolerant(page, lambda: login.phase(prefer_sms=self.state.sms_tab))
        phase = phase or LoginPhase.UNKNOWN
        if (
            phase is LoginPhase.SMS
            and not self.state.sms_tab
            and not self.state.sms_action
            and self._tolerant(page, login.secondary_sms_visible) is not True
        ):
            # Ordinary SMS controls remain beside the QR/confirmation panel. Their
            # presence alone does not require a QR user to switch login methods.
            phase = LoginPhase.UNKNOWN
        if phase in (LoginPhase.QR, LoginPhase.SCANNED, LoginPhase.CAPTCHA):
            # A submitted code whose page visibly left the SMS flow (captcha, QR reset)
            # must not wedge sms_action in "waiting": that would 409-block both SMS
            # buttons for the rest of the session. UNKNOWN is excluded on purpose — it
            # also covers a page that is merely mid-navigation after a submit.
            action = self.state.snapshot()["sms_action"] or {}
            if (
                action.get("kind") == "sms_verify"
                and action.get("status") == "waiting"
                and self._verify_started_at is not None
                and self.clock() - self._verify_started_at >= VERIFY_RESULT_SECONDS
            ):
                self._warn(
                    "验证页已离开短信流程，本次验证未完成；请按页面提示重新操作",
                    status="unconfirmed",
                )
        if phase is LoginPhase.CAPTCHA:
            self.state.update(stage="verify")
            screen = self._tolerant(page, login.screenshot)
            if screen:
                self.state.set_image("screen", screen)
            link = self._novnc()
            hint = (
                "可打开 noVNC 链接手动完成验证。"
                if link
                else "请取消本次登录，本机改用 --mode window 手动验证；云端改用 --mode vnc。"
            )
            self.state.update(
                status=Status.VERIFICATION_REQUIRED,
                message=f"抖音要求完成安全验证（滑块或图形），无法通过本页代为完成。{hint}",
                novnc_url=link,
            )
        elif phase is LoginPhase.SMS:
            self.state.update(stage="verify")
            if not self.state.screen_version or self.state.status is not Status.SMS_REQUIRED:
                self._capture_sms(page, login)
            phone_input = bool(self._tolerant(page, login.phone_input_visible))
            feedback = self._tolerant(page, login.sms_feedback)
            phone_hint = self._tolerant(page, login.phone_hint)
            resend_in = self._tolerant(page, login.sms_resend_in) or 0
            if feedback == "短信已发送至":
                feedback = f"短信已发送至 {phone_hint or '账号绑定的手机'}，请输入验证码"
            default = (
                "请输入手机号并获取验证码"
                if phone_input
                else "抖音要求短信验证：点击「获取验证码」，再输入手机收到的验证码"
            )
            if self._tolerant(page, login.sms_choice_visible):
                default = "抖音要求验证身份：点击「获取验证码」使用官方「接收短信验证码」方式"
            action = self.state.snapshot()["sms_action"] or {}
            if action.get("kind") == "sms_verify" and action.get("status") == "waiting":
                if feedback and any(text in feedback for text in sel.SMS_ERROR_TEXTS):
                    self._warn(feedback)
                    self._capture_sms(page, login)
                elif self._verify_started_at is not None and (
                    self.clock() - self._verify_started_at >= VERIFY_RESULT_SECONDS
                ):
                    warning = (
                        "抖音仍停留在短信验证页，尚未确认验证成功。请查看下方验证页当前画面和提示。"
                    )
                    self._warn(warning, status="unconfirmed")
                    self._capture_sms(page, login)
                else:
                    feedback = "已点击抖音验证按钮，正在等待验证结果…"
            elif action.get("status") in ("queued", "running"):
                feedback = action.get("message")
            self.state.update(
                status=Status.SMS_REQUIRED,
                phone_input=phone_input,
                secondary_verification=bool(self._tolerant(page, login.secondary_sms_visible)),
                phone_hint=phone_hint,
                sms_resend_in=resend_in,
                message=self._current_notice() or feedback or default,
            )
        elif phase is LoginPhase.SCANNED:
            self.state.update(
                status=Status.SCANNED,
                stage="confirm",
                sms_resend_in=0,
                message="已扫码，请在抖音 App 中确认登录",
            )
        # LoginPhase.UNKNOWN (mid-navigation after a scan or submit) matches no
        # branch on purpose: keep the last observed status and images so a
        # verification prompt is never flashed over with "等待扫码".
        elif phase is LoginPhase.QR:
            if self.clock() >= self._qr_auto_retry_at:
                try:
                    if login.refresh_if_expired():
                        self._notice = None
                except Exception:
                    if page.is_closed():
                        raise LoginError("浏览器窗口已关闭，登录中止") from None
                    self._warn("二维码自动刷新未成功，请点击「刷新二维码」重试")
                    self._qr_auto_retry_at = self.clock() + 10
            png = self._tolerant(page, login.qr_png)
            if png:
                self.state.set_image("qr", png)
            self.state.update(
                status=Status.PENDING,
                stage="scan" if png else "loading",
                phone_input=True,
                secondary_verification=False,
                phone_hint=None,
                sms_resend_in=0,
                message=self._current_notice() or ("" if png else "正在获取二维码…"),
            )

    def _run_commands(self, page: Page, login: LoginPage) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            if command.kind == "cancel":
                raise LoginCancelled("登录已在网页上取消")
            if command.kind in ("sms_send", "sms_verify"):
                self._notice = None
                self.state.update(
                    sms_action={
                        "kind": command.kind,
                        "status": "running",
                        "message": "正在操作抖音验证页面…",
                    }
                )
            try:
                self._execute(login, command)
            except LoginError as exc:
                self._warn(str(exc))
                self._capture_sms(page, login)
            except Exception as exc:
                if page.is_closed():
                    raise LoginError("浏览器窗口已关闭，登录中止") from exc
                log.debug("login command %s failed", command.kind)
                self._warn("操作没有成功，页面可能还在加载，请稍后再试")
                self._capture_sms(page, login)
            finally:
                if command.kind == "qr_refresh":
                    self.state.update(qr_refreshing=False)

    def _capture_sms(self, page: Page, login: LoginPage) -> None:
        screen = self._tolerant(page, login.screenshot)
        if screen:
            self.state.set_image("screen", screen)
        diagnostics = self._tolerant(page, login.sms_diagnostics)
        if diagnostics:
            self.state.update(sms_diagnostics=diagnostics)

    def _warn(self, text: str, *, status: str = "error") -> None:
        """Show a command failure; page observations must not hide it at once."""
        self._notice = text
        action = self.state.snapshot()["sms_action"]
        changes = {"message": text}
        if action:
            changes["sms_action"] = {"kind": action["kind"], "status": status, "message": text}
        self.state.update(**changes)
        log.warning("login action: %s", text)

    def _current_notice(self) -> str | None:
        return self._notice

    def _execute(self, login: LoginPage, command: Command) -> None:
        if command.kind in ("sms_send", "sms_verify", "qr_refresh"):
            self._notice = None
        phase = login.phase(prefer_sms=self.state.sms_tab)
        secondary = login.secondary_sms_visible()
        if command.kind in ("sms_send", "sms_verify", "use_qr") and phase in (
            LoginPhase.CAPTCHA,
            LoginPhase.SCANNED,
        ):
            raise LoginError("登录阶段已变化，请按页面提示完成验证")
        if command.kind == "qr_refresh":
            login.refresh_qr()
            png = login.qr_png()
            if png:
                self.state.set_image("qr", png)
            self.state.update(message="二维码已刷新，请用抖音 App 扫码")
            log.info("login command qr_refresh: official QR refreshed")
        elif command.kind == "use_qr":
            if secondary:
                raise LoginError("请先完成扫码后的二次验证")
            login.use_qr_tab()
            self.state.update(sms_tab=False, message="")
        elif command.kind == "sms_send":
            phone = command.value
            if secondary and not login.phone_input_visible():
                phone = None  # stale phone-form input must not replace the bound account
            if phone and phase is not LoginPhase.SMS:
                if not login.use_sms_tab():
                    raise LoginError("找不到「验证码登录」入口")
                self.state.update(sms_tab=True)
            login.request_code(phone)
            self.state.update(
                phone=mask_phone(phone) if phone else self.state.phone,
                message=login.sms_feedback() or "已请求验证码，请查收短信",
                sms_action={"kind": "sms_send", "status": "done", "message": "已操作短信获取按钮"},
            )
            log.info("login command sms_send: platform control clicked")
        elif command.kind == "sms_verify" and command.value:
            if phase is not LoginPhase.SMS:
                raise LoginError("当前页面未要求短信验证码")
            login.submit_code(command.value)
            self._verify_started_at = self.clock()
            self.state.update(
                message="已点击抖音验证按钮，正在等待验证结果…",
                sms_action={
                    "kind": "sms_verify",
                    "status": "waiting",
                    "message": "等待抖音验证结果",
                },
            )
            log.info("login command sms_verify: platform control clicked")

    def _novnc(self) -> str | None:
        if self.state.novnc_url or self._vnc_tried:
            return self.state.novnc_url
        self._vnc_tried = True
        if self.settings.login.headless or not self.settings.login.vnc_password:
            return None
        try:
            self._stack.enter_context(vnc_bridge(self.settings))
        except LoginError as exc:
            log.warning("noVNC fallback unavailable: %s", exc)
            return None
        host = urlsplit(self.state.url or "").hostname
        return novnc_url(self.settings, host)

    @staticmethod
    def _tolerant(page: Page, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except Exception as exc:
            if page.is_closed():
                raise LoginError("浏览器窗口已关闭，登录中止") from exc
            log.debug("login page busy: %s", exc)
            return None


# ------------------------------------------------------------------------ flow
def announce(url: str, minutes: float) -> None:
    """Print the link and a terminal QR code of it (no-op without a console)."""
    import sys

    if sys.stdout is None:
        return
    print(f"[login] 在手机或电脑浏览器打开（{minutes:.0f} 分钟内有效，仅本次可用）：\n  {url}")
    drawing = termqr.render(url, getattr(sys.stdout, "encoding", None) or "utf-8")
    if drawing:
        print("[login] 也可以用手机相机扫描下面的二维码打开该链接：")
        print(drawing)
    sys.stdout.flush()


def web_login(
    settings: Settings,
    account: Account,
    notifier: Notifier,
    *,
    timeout_s: float | None = None,
    session_dir: Path | None = None,
    linger_s: float = LINGER_SECONDS,
    notify_success: bool = True,
) -> LoginRecord:
    """Run one relay session; returns the login record or raises :class:`LoginError`."""
    timeout_s = timeout_s or settings.login.timeout_seconds
    session_id = session_dir.name if session_dir else secrets.token_hex(6)
    expires_at = datetime.now(settings.zone) + timedelta(seconds=timeout_s)
    state = RelayState(
        session_id, account.name, expires_at, proxy=mask_proxy(account.proxy) or None
    )
    files = SessionFiles(session_dir) if session_dir else None
    if files:
        state.subscribe(files)
    commands: queue.Queue[Command] = queue.Queue()
    relay = RelayServer(state, commands, settings.login)
    internal = settings.login.port == 0
    if settings.cloud and not settings.login.public_url and not internal:
        log.warning("login.public_url is not set: the link may not be reachable from a phone")

    with relay.running():
        state.update(url=relay.url)
        if not internal:
            announce(relay.url, timeout_s / 60)
            notifier.push(
                "请打开链接登录抖音",
                f"账号 {account.name} 的登录页（{timeout_s / 60:.0f} 分钟内有效，仅本次可用）：\n\n"
                f"[{relay.url}]({relay.url})",
                redact=(relay.token,),
                dedupe=False,
            )
        driver = WebLoginDriver(
            settings,
            state,
            commands,
            deadline=time.monotonic() + timeout_s,
            cancel_requested=lambda: (
                relay.cancelled.is_set() or bool(files and files.cancel_requested())
            ),
        )
        try:
            with open_context(
                settings, account, on_screen=False, headless=settings.login.headless
            ) as context:
                state.update(stage="connection", message="正在检查网络连接…")
                initial_ip = initial_login_ip(settings, account, context)
                page = context.pages[0] if context.pages else context.new_page()
                chat = driver.drive(page)
                with relay._operation_lock:
                    if driver.cancel_requested():
                        raise LoginCancelled("登录已取消")
                    state.update(
                        status=Status.FINALIZING,
                        stage="save",
                        message="已通过抖音验证，正在核对出口 IP 并保存登录…",
                    )
                record = complete_login(
                    settings,
                    account,
                    context,
                    chat,
                    notifier,
                    initial_ip=initial_ip,
                    notify_success=notify_success,
                )
        except LoginCancelled as exc:
            state.update(status=Status.CANCELLED, message=str(exc))
            _linger(linger_s)
            raise
        except LoginExpired:
            state.update(
                status=Status.EXPIRED, message=f"{timeout_s / 60:.0f} 分钟内未完成登录，链接已失效"
            )
            _linger(linger_s)
            raise LoginError(f"{timeout_s:.0f} 秒内未完成登录") from None
        except Exception as exc:
            text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            state.update(status=Status.FAILED, message=text)
            _linger(linger_s)
            raise
        finally:
            driver.close()
        state.update(
            status=Status.SUCCESS,
            message="已有登录状态有效，已重新核对账号环境"
            if state.login_source == "saved_session"
            else f"登录成功，出口 IP {record.egress_ip}",
            egress_ip=record.egress_ip,
        )
        _linger(linger_s)
    return record


def _linger(seconds: float) -> None:
    with suppress(KeyboardInterrupt):
        time.sleep(seconds)
