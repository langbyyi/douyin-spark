"""Interactive login that pins the account to the current environment.

Modes:
  window  a visible browser window on this machine (local PC)
  qr      headed browser on a virtual display; the QR code is saved to disk and
          pushed via Server酱 (served from a short-lived HTTP endpoint when
          ``login.public_url`` is set)
  vnc     like ``window`` but on a server: a password-protected noVNC session
          that exists only while the login is in progress
  web     a one-time web page relays the live QR code and a phone + SMS code
          form to the browser running here (see :mod:`dyspark.weblogin`)

Whatever the mode, the browser runs on this machine with the account's own
profile and proxy, so the egress IP recorded at the end is the one later runs use.
"""

from __future__ import annotations

import hashlib
import http.server
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit

from dyspark import egress, httpd
from dyspark import selectors as sel
from dyspark.account_health import HealthStore
from dyspark.avatars import validate_avatar_url
from dyspark.browser import open_context
from dyspark.chat import JS_VISIBLE_TEXT, ChatPage, PageState
from dyspark.config import Account, Settings
from dyspark.notify import Notifier
from dyspark.store import DataDir, LoginRecord, mask_proxy, proxy_fingerprint, write_private_bytes

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page

log = logging.getLogger(__name__)

T = TypeVar("T")
MAX_QR_PUSHES = 4
POLL_SECONDS = 2.0


class LoginMode(StrEnum):
    WINDOW = "window"
    QR = "qr"
    VNC = "vnc"
    WEB = "web"


class LoginError(RuntimeError):
    pass


class LoginPhase(StrEnum):
    """What the login panel is currently asking for."""

    QR = "qr"  # a QR code waiting to be scanned
    SCANNED = "scanned"  # scanned, waiting for confirmation on the phone
    SMS = "sms"  # an SMS code field is on screen (SMS tab or post-scan check)
    CAPTCHA = "captcha"  # slider / picture verification: needs a human on the page
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- QR page
def mask_phone(phone: str) -> str:
    """Display form shared by SMS hints and the relay's masked account phone."""
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "****"


def phone_hint_from_text(text: str) -> str | None:
    """Return only an unambiguous redacted number or explicit phone suffix."""
    compact = re.sub(r"[^\S\r\n]+", "", text)
    compact = re.sub(r"(?:\+86|(?<!\d)0086)(?=1)", "", compact)
    hints = {
        f"{match[1]}{'*' * len(match[2])}{match[3]}"
        for match in re.finditer(r"(?<!\d)(1\d{2})([*＊•●×xX]{3,8})(\d{1,4})(?!\d)", compact)
        if len(match[1]) + len(match[2]) + len(match[3]) == 11
    }
    if any(word in compact for word in ("手机", "电话", "绑定", "发送至", "发送到")):
        hints.update(
            mask_phone(match[1]) for match in re.finditer(r"(?<!\d)(1\d{10})(?!\d)", compact)
        )
    tails = set(re.findall(r"尾号(?:为|是|[:：])?(\d{4})(?!\d)", compact))
    if len(hints) == 1 and all(next(iter(hints)).endswith(tail) for tail in tails):
        return next(iter(hints))
    if not hints and len(tails) == 1:
        return "尾号 " + next(iter(tails))
    return None


class LoginPage:
    """Login-panel interactions (opening the panel, reading/refreshing the QR code)."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def open_panel(self) -> None:
        """Open the login panel and switch to the QR tab (Douyin often defaults to SMS)."""
        if not sel.visible_login_qr(self.page) and not self._visible((sel.LOGIN_PANEL,)):
            entry = self.page.get_by_text(sel.LOGIN_ENTRY_TEXT)
            if entry.count():
                entry.first.click()
                self.page.wait_for_timeout(1500)
        # Always try the QR tab: a visible SMS panel used to short-circuit this.
        if not sel.visible_login_qr(self.page):
            tab = self.page.get_by_text(sel.QR_TAB_TEXT)
            if tab.count() and tab.first.is_visible():
                tab.first.click()
                self.page.wait_for_timeout(1000)

    def _visible(self, candidates: tuple[str, ...], root=None):
        for candidate in candidates:
            locator = (root or self.page).locator(candidate)
            for index in range(min(locator.count(), 5)):
                if locator.nth(index).is_visible():
                    return locator.nth(index)
        return None

    def qr_png(self) -> bytes | None:
        """Screenshot a QR image; verification panels are not QR codes."""
        if self.phase() is not LoginPhase.QR:
            return None
        image = sel.visible_login_qr(self.page)
        return image.screenshot() if image is not None else None

    def refresh_if_expired(self) -> bool:
        if self.phase() is not LoginPhase.QR or not self._visible(sel.QR_EXPIRED):
            return False
        log.info("QR code expired, refreshing")
        self.refresh_qr()
        return True

    def refresh_qr(self) -> None:
        """Refresh the official QR in this session; never abandon post-scan verification."""
        if self.phase() is not LoginPhase.QR:
            raise LoginError("当前阶段不能刷新二维码，请按页面提示完成验证")
        image = sel.visible_login_qr(self.page)
        # The official page can replace the image entirely with its expiry control.
        source = image or self._visible(sel.QR_EXPIRED)
        if source is None:
            raise LoginError("二维码刷新入口尚未就绪，请稍后重试")
        root = source.locator("..")
        refresh = None
        for _ in range(8):
            if self._visible(sel.QR_EXPIRED, root):
                refresh = self._control_in(root, sel.QR_REFRESH_TEXT)
                break
            if root.evaluate("el => el.tagName === 'BODY'"):
                break
            root = root.locator("..")
        if refresh is not None:
            refresh.click(timeout=6000)
        else:
            self.page.reload(wait_until="domcontentloaded")
            self.open_panel()
        self.page.wait_for_timeout(1500)
        if self._visible(sel.QR_EXPIRED):
            raise LoginError("抖音二维码仍显示失效，刷新尚未成功，请稍后重试")
        if self.phase() is LoginPhase.UNKNOWN:
            raise LoginError("刷新后二维码尚未就绪，请稍后重试")

    # ------------------------------------------------------------- state probes
    def visible_text(self, texts: tuple[str, ...]) -> str | None:
        # Navigation can finish between the chat-state probe and the login probe.
        # Conversation previews and message bodies must never classify login state.
        return self.page.evaluate(
            JS_VISIBLE_TEXT, [list(texts), [sel.CONVERSATION_ROW, sel.MESSAGE_BUBBLE]]
        )

    def phase(self, *, prefer_sms: bool = False) -> LoginPhase:
        """Classify the panel.

        Douyin can show QR and SMS side by side. Start with QR, then follow the
        explicitly selected SMS flow while its input controls remain visible.
        """
        if self._visible(tuple(s for s in sel.VERIFY_ELEMENTS if not s.startswith("iframe"))):
            return LoginPhase.CAPTCHA
        # Post-scan SMS dialogs can say "安全验证" and leave the QR visible behind them.
        secondary = self._secondary_sms_root()
        if secondary is not None:
            if self._visible(sel.VERIFY_ELEMENTS, secondary):
                return LoginPhase.CAPTCHA
            return LoginPhase.SMS
        if self._visible(sel.VERIFY_ELEMENTS):
            return LoginPhase.CAPTCHA
        if self.visible_text(sel.VERIFY_TEXTS):
            return LoginPhase.CAPTCHA
        if self.visible_text(sel.QR_SCANNED_TEXTS):
            return LoginPhase.SCANNED
        if prefer_sms and (self._visible(sel.CODE_INPUT) or self._visible(sel.PHONE_INPUT)):
            return LoginPhase.SMS
        if sel.visible_login_qr(self.page) or self._visible(sel.QR_EXPIRED):
            return LoginPhase.QR
        if self._visible(sel.CODE_INPUT) or self._visible(sel.PHONE_INPUT):
            return LoginPhase.SMS
        return LoginPhase.UNKNOWN

    def phone_input_visible(self) -> bool:
        return self._sms_input(sel.PHONE_INPUT) is not None

    def secondary_sms_visible(self) -> bool:
        return self._secondary_sms_root() is not None

    def sms_choice_visible(self) -> bool:
        root = self._secondary_sms_root()
        if root is None or self._sms_input(sel.CODE_INPUT) is not None:
            return False
        return any(item.is_visible() for item in root.get_by_text(sel.SMS_RECEIVE_TEXT).all())

    def phone_hint(self) -> str | None:
        """Read platform hints, excluding typed values and background conversations."""
        root = self._sms_root()
        if root is self.page:
            root = self._visible((sel.LOGIN_PANEL,))
        if root is None:
            return None
        root = self._phone_hint_root(root)
        text = root.inner_text()
        inputs = root.locator(", ".join(sel.PHONE_INPUT))
        for index in range(min(inputs.count(), 10)):
            box = inputs.nth(index)
            if box.is_visible():
                text += "\n" + (box.get_attribute("placeholder") or "")
        return phone_hint_from_text(text)

    def _secondary_sms_root(self):
        """Find the active SMS container, including visible embedded verification pages."""
        contexts = []
        for frame in reversed(self.page.frames):
            if frame is self.page.main_frame:
                contexts.append(frame)
                continue
            ancestor = frame
            while ancestor.parent_frame is not None:
                if not ancestor.frame_element().is_visible():
                    break
                ancestor = ancestor.parent_frame
            else:
                contexts.append(frame)
        for context in contexts:
            inputs = context.locator(", ".join(sel.CODE_INPUT))
            for index in reversed(range(min(inputs.count(), 10))):
                box = inputs.nth(index)
                if not box.is_visible():
                    continue
                parents = box.locator("xpath=ancestor::*")
                fallback = None
                for parent_index in reversed(range(max(0, parents.count() - 12), parents.count())):
                    root = parents.nth(parent_index)
                    if root.evaluate("el => ['BODY', 'HTML'].includes(el.tagName)"):
                        continue
                    if any(text in root.inner_text() for text in sel.SMS_VERIFY_TEXTS):
                        if fallback is None:
                            fallback = root
                        if self._control_in(root, sel.SUBMIT_TEXT) is not None:
                            return root
                if fallback is not None:
                    return fallback
            choices = context.get_by_text(sel.SMS_RECEIVE_TEXT)
            for index in reversed(range(min(choices.count(), 10))):
                choice = choices.nth(index)
                if not choice.is_visible() or choice.evaluate("el => /^H[1-6]$/.test(el.tagName)"):
                    continue
                if choice.evaluate(
                    "(el, excluded) => excluded.some(s => el.closest(s))",
                    [sel.CONVERSATION_ROW, sel.MESSAGE_BUBBLE],
                ):
                    continue
                return choice.locator("xpath=..")
        return None

    def _phone_hint_root(self, root):
        # Number hints can sit outside the controls; keep action locators narrow.
        dialog = root.locator("xpath=ancestor-or-self::*[@role='dialog' or @aria-modal='true'][1]")
        if dialog.count():
            return dialog
        fallback = root
        for _ in range(12):
            if root.evaluate("el => ['BODY', 'HTML'].includes(el.tagName)"):
                break
            if root.locator(f"{sel.CONVERSATION_ROW}, {sel.MESSAGE_BUBBLE}").count():
                break
            text = root.inner_text()
            if any(label in text for label in sel.VERIFY_TEXTS):
                return root
            root = root.locator("xpath=..")
        return fallback

    def _sms_root(self):
        root = self._secondary_sms_root()
        return root if root is not None else self._visible((sel.LOGIN_PANEL,)) or self.page

    def _sms_input(self, candidates: tuple[str, ...]):
        inputs = self._sms_root().locator(", ".join(candidates))
        for index in reversed(range(min(inputs.count(), 10))):
            if inputs.nth(index).is_visible():
                return inputs.nth(index)
        return None

    def sms_feedback(self) -> str | None:
        """An error or "code sent" notice shown by the SMS form, if any."""
        root = self._sms_root()
        if root is self.page:
            return self.visible_text((*sel.SMS_ERROR_TEXTS, *sel.SMS_SENT_TEXTS))
        text = root.inner_text()
        return next(
            (value for value in (*sel.SMS_ERROR_TEXTS, *sel.SMS_SENT_TEXTS) if value in text), None
        )

    def sms_resend_in(self) -> int:
        root = self._sms_root()
        text = root.locator("body").inner_text() if root is self.page else root.inner_text()
        match = re.search(r"(\d{1,3})\s*(?:s|秒)\s*后?\s*(?:重新发送|重新获取)", text)
        return min(600, int(match[1])) if match else 0

    def screenshot(self) -> bytes:
        return self.page.screenshot(mask=[frame.locator("input") for frame in self.page.frames])

    def sms_diagnostics(self) -> dict:
        """Describe controls without recording input values, page URLs or credentials."""
        root = self._sms_root()

        def safe(text):
            return re.sub(r"\d{4,}", "<redacted>", text or "")[:100]

        inputs = root.locator("input")
        buttons = root.get_by_role("button")
        return {
            "frame_count": len(self.page.frames),
            "inputs": [
                {
                    name: safe(inputs.nth(i).get_attribute(name))
                    for name in ("type", "placeholder", "autocomplete", "maxlength")
                }
                for i in range(min(inputs.count(), 10))
                if inputs.nth(i).is_visible()
            ],
            "buttons": [
                {"text": safe(buttons.nth(i).inner_text()), "enabled": buttons.nth(i).is_enabled()}
                for i in range(min(buttons.count(), 10))
                if buttons.nth(i).is_visible()
            ],
        }

    # ------------------------------------------------------------ SMS actions
    def use_sms_tab(self) -> bool:
        return self._click_tab(sel.SMS_TAB_TEXT)

    def use_qr_tab(self) -> bool:
        return self._click_tab(sel.QR_TAB_TEXT)

    def request_code(self, phone: str | None) -> None:
        """Type the phone number (when the form asks for one) and ask for a code."""
        if self._sms_input(sel.CODE_INPUT) is None and self.secondary_sms_visible():
            receive = self._last_visible_control(sel.SMS_RECEIVE_TEXT)
            if receive is not None:
                receive.click()
                self.page.wait_for_timeout(1000)
                return
        if phone:
            box = self._sms_input(sel.PHONE_INPUT)
            if box is None:
                raise LoginError("页面上没有手机号输入框")
            self._type(box, phone)
        self._agree_to_terms()
        button = self._last_visible_control(sel.SEND_CODE_TEXT)
        if button is None:
            raise LoginError("找不到「获取验证码」按钮")
        button.click()
        self.page.wait_for_timeout(1000)

    def submit_code(self, code: str) -> None:
        box = self._sms_input(sel.CODE_INPUT)
        if box is None:
            raise LoginError("页面上没有验证码输入框")
        self._type(box, code)
        self._agree_to_terms()
        button = self._last_visible_control(sel.SUBMIT_TEXT)
        if button is None:
            raise LoginError("找不到登录/验证按钮")
        if not button.is_enabled():
            raise LoginError("抖音验证按钮暂不可用，请检查验证码是否填写完整")
        button.click(timeout=6000)
        self.page.wait_for_timeout(1500)

    def _click_tab(self, pattern) -> bool:
        tab = self.page.get_by_text(pattern)
        for index in range(min(tab.count(), 5)):
            if tab.nth(index).is_visible():
                tab.nth(index).click()
                self.page.wait_for_timeout(800)
                return True
        return False

    def _type(self, box: Locator, text: str) -> None:
        box.click()
        box.fill("")
        box.press_sequentially(text, delay=80)

    def _agree_to_terms(self) -> None:
        boxes = self._sms_root().locator(sel.AGREEMENT_CHECKBOX)
        for index in range(min(boxes.count(), 3)):
            box = boxes.nth(index)
            if box.is_visible() and not box.is_checked():
                box.check()

    def _last_visible_control(self, pattern) -> Locator | None:
        """Buttons first; dialogs are appended late in the DOM, so prefer the last one."""
        return self._control_in(self._sms_root(), pattern)

    @staticmethod
    def _control_in(root, pattern) -> Locator | None:
        candidates = (root.get_by_role("button", name=pattern), root.get_by_text(pattern))
        for locator in candidates:
            visible = [locator.nth(i) for i in range(min(locator.count(), 8))]
            visible = [
                item
                for item in visible
                if item.is_visible() and not item.evaluate("el => /^H[1-6]$/.test(el.tagName)")
            ]
            if visible:
                return visible[-1]
        return None


# ------------------------------------------------------------------------ publishing
class QrPublisher:
    """Saves QR screenshots and pushes them, optionally via a temporary HTTP link."""

    def __init__(self, settings: Settings, notifier: Notifier, path: Path) -> None:
        self.settings = settings
        self.notifier = notifier
        self.path = path
        self.token = secrets.token_urlsafe(16)
        self.pushes = 0
        self._last_digest = ""
        self.serving_link = False

    @property
    def link(self) -> str | None:
        base = self.settings.login.public_url
        if not base or not self.serving_link:
            return None
        return f"{base.rstrip('/')}/{self.token}.png"

    def publish(self, png: bytes) -> None:
        digest = hashlib.sha256(png).hexdigest()
        if digest == self._last_digest:
            return
        self._last_digest = digest
        # Atomic + 0600: the temporary HTTP endpoint serves this exact file while the
        # poller may rewrite it, so a non-atomic write could push a torn PNG.
        write_private_bytes(self.path, png)
        if self.pushes >= MAX_QR_PUSHES:
            return
        self.pushes += 1
        lines = [f"二维码已保存: `{self.path}`", "请用抖音 App 扫一扫（可从相册识别）。"]
        if self.link:
            lines.insert(0, f"![登录二维码]({self.link})\n\n[打开二维码]({self.link})")
        print(f"[login] QR saved to {self.path}" + (f" | {self.link}" if self.link else ""))
        self.notifier.push("请扫码登录", "\n\n".join(lines), redact=(self.token,), dedupe=False)

    @contextmanager
    def serving(self) -> Iterator[None]:
        if not self.settings.login.public_url:
            yield
            return
        publisher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "dyspark"
            sys_version = ""

            def do_GET(self) -> None:
                if self.path != f"/{publisher.token}.png" or not publisher.path.exists():
                    self.send_error(404)
                    return
                data = publisher.path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args) -> None:
                return

        options = self.settings.login
        try:
            server = httpd.bind_server(options.bind, options.port, Handler)
        except OSError as exc:
            log.warning(
                "cannot serve the QR link on port %d (%s); file/push only", options.port, exc
            )
            self.serving_link = False
            yield
            return
        self.serving_link = True
        log.info("temporary QR endpoint listening on :%d", options.port)
        with httpd.serving(server):
            yield


@contextmanager
def vnc_bridge(settings: Settings) -> Iterator[None]:
    """Run x11vnc + websockify (noVNC) for the duration of the login only."""
    options = settings.login
    if not options.vnc_password:
        raise LoginError("vnc 模式需要在 .env 中设置 SPARK_VNC_PASSWORD")
    if len(options.vnc_password) < 6:
        raise LoginError("SPARK_VNC_PASSWORD 至少 6 位")
    for tool in ("x11vnc", "websockify"):
        if not shutil.which(tool):
            raise LoginError(f"vnc 模式需要 {tool}（Docker 镜像中已包含）")
    if not os.environ.get("DISPLAY"):
        raise LoginError("vnc 模式需要虚拟显示器：请在 xvfb-run 下运行（Docker 入口脚本已处理）")
    workdir = Path(tempfile.mkdtemp(prefix="dyspark-vnc-"))
    processes: list[subprocess.Popen[bytes]] = []
    try:
        passfile = workdir / "passwd"
        stored = subprocess.run(
            ["x11vnc", "-storepasswd", options.vnc_password, str(passfile)],
            capture_output=True,
            check=False,
        )
        if stored.returncode != 0 or not passfile.exists():
            raise LoginError("x11vnc 无法保存 VNC 密码")
        vnc_args = ["x11vnc", "-display", os.environ["DISPLAY"], "-rfbauth", str(passfile)]
        vnc_args += ["-rfbport", "5900", "-localhost", "-forever", "-shared", "-quiet"]
        processes.append(subprocess.Popen(vnc_args))
        web_root = next(
            (p for p in ("/usr/share/novnc", "/usr/share/noVNC") if Path(p).is_dir()), None
        )
        web_args = ["websockify", str(options.vnc_port), "localhost:5900"]
        if web_root:
            web_args[1:1] = ["--web", web_root]
        processes.append(subprocess.Popen(web_args))
        time.sleep(1.0)
        if any(proc.poll() is not None for proc in processes):
            raise LoginError("x11vnc / websockify 启动失败（端口被占用？）")
        print(f"[login] noVNC: {novnc_url(settings)} （密码见 SPARK_VNC_PASSWORD）")
        yield
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def novnc_url(settings: Settings, host: str | None = None) -> str:
    public = settings.login.public_url
    host = host or (urlsplit(public).hostname if public else None) or "<服务器IP>"
    return f"http://{host}:{settings.login.vnc_port}/vnc.html"


# ----------------------------------------------------------------------------- flow
def login_account(
    settings: Settings,
    account: Account,
    mode: LoginMode,
    notifier: Notifier,
    timeout_s: float | None = None,
    *,
    session_dir: Path | None = None,
    notify_success: bool = True,
) -> LoginRecord:
    """Log in interactively and record the egress IP / proxy / profile used.

    ``session_dir`` (web mode) receives ``session.json`` and the QR/screen images,
    which is how the MCP server follows a login running as a background job.
    """
    timeout_s = timeout_s or settings.login.timeout_seconds
    if mode is LoginMode.WEB:
        from dyspark.weblogin import web_login

        return web_login(
            settings,
            account,
            notifier,
            timeout_s=timeout_s,
            session_dir=session_dir,
            notify_success=notify_success,
        )
    data = DataDir(settings.data_dir)
    qr_path = data.account_dir(account.name) / "login-qr.png"
    publisher = QrPublisher(settings, notifier, qr_path)
    bridge = vnc_bridge(settings) if mode is LoginMode.VNC else _noop()

    try:
        with (
            bridge,
            publisher.serving(),
            open_context(settings, account, on_screen=True) as context,
        ):
            initial_ip = initial_login_ip(settings, account, context)
            page = context.pages[0] if context.pages else context.new_page()
            chat = ChatPage(page)
            chat.open()
            if chat.state(wait_s=20) is not PageState.READY:
                _wait_for_scan(chat, LoginPage(page), mode, publisher, notifier, timeout_s)
            return complete_login(
                settings,
                account,
                context,
                chat,
                notifier,
                initial_ip=initial_ip,
                notify_success=notify_success,
            )
    finally:
        qr_path.unlink(missing_ok=True)


def initial_login_ip(settings: Settings, account: Account, context: BrowserContext) -> str:
    """Establish this login attempt's route before interacting with the login page."""
    try:
        return egress.lookup(context, settings.ip_echo_urls)
    except egress.EgressError as exc:
        HealthStore(settings.data_dir).record(
            account.name,
            "network_error",
            f"登录前无法确认出口IP: {exc}",
            current_ip=None,
            proxy_fingerprint=proxy_fingerprint(account.proxy),
        )
        raise LoginError(f"登录前无法确认出口IP: {exc}；未保存登录记录") from exc


def complete_login(
    settings: Settings,
    account: Account,
    context: BrowserContext,
    chat: ChatPage,
    notifier: Notifier,
    *,
    initial_ip: str | None = None,
    notify_success: bool = True,
) -> LoginRecord:
    """After the page reports a login: verify it, look up the egress IP and pin it."""
    health = HealthStore(settings.data_dir)
    chat.page.wait_for_timeout(5000)  # let the session cookies settle
    state = chat.state(wait_s=15)
    if state is not PageState.READY:
        health.pause(
            account.name,
            "登录后聊天页没有就绪，请重试",
            {PageState.VERIFY: "blocked", PageState.LOGGED_OUT: "logged_out"}.get(
                state, "network_error"
            ),
        )
        raise LoginError("登录后聊天页没有就绪，请重试")
    try:
        address = egress.lookup(context, settings.ip_echo_urls)
    except egress.EgressError as exc:
        health.record(
            account.name,
            "network_error",
            f"已登录，但{exc}；未保存登录记录",
            current_ip=None,
            proxy_fingerprint=proxy_fingerprint(account.proxy),
        )
        raise LoginError(f"已登录，但{exc}；未保存登录记录，请检查网络后重试") from exc
    data = DataDir(settings.data_dir)
    if initial_ip is not None and not egress.ip_matches(initial_ip, address, settings.ip_policy):
        previous = LoginRecord.load(data.login_file(account.name))
        note = f"登录期间出口IP由 {initial_ip} 变为 {address}；未更新登录记录，请稳定网络后重试"
        health.record(
            account.name,
            "ip_mismatch",
            note,
            current_ip=address,
            login_ip=previous.egress_ip if previous else initial_ip,
            proxy_fingerprint=proxy_fingerprint(account.proxy),
        )
        raise LoginError(note)
    from dyspark.account_profile import OwnProfile

    profile = getattr(chat, "own_profile", lambda: None)()
    profile = profile if isinstance(profile, OwnProfile) else None
    previous = LoginRecord.load(data.login_file(account.name))
    if profile is None or not profile.user_id:
        note = "暂时无法核实抖音账号身份；未更新登录记录，请稍后重试"
        health.record(
            account.name,
            "network_error",
            note,
            current_ip=address,
            login_ip=previous.egress_ip if previous else None,
            proxy_fingerprint=proxy_fingerprint(account.proxy),
        )
        raise LoginError(note)
    if previous and previous.user_id and previous.user_id != profile.user_id:
        note = "当前抖音账号与原绑定不同；未更新登录记录，请切换回原账号后重新登录"
        health.record(
            account.name,
            "blocked",
            note,
            current_ip=address,
            login_ip=previous.egress_ip,
            proxy_fingerprint=proxy_fingerprint(account.proxy),
        )
        raise LoginError(note)
    record = LoginRecord(
        account=account.name,
        egress_ip=address,
        proxy=mask_proxy(account.proxy),
        proxy_fingerprint=proxy_fingerprint(account.proxy),
        profile_dir=str(data.profile_dir(account.name)),
        host=platform.node(),
        environment="cloud" if settings.cloud else "local",
        logged_in_at=datetime.now(settings.zone).isoformat(timespec="seconds"),
        nickname=profile.nickname if profile else "",
        user_id=profile.user_id if profile else "",
        avatar_url=validate_avatar_url(profile.avatar_url) if profile else "",
        profile_updated_at=datetime.now(settings.zone).isoformat(timespec="seconds")
        if profile
        else "",
    )
    record.save(data.login_file(account.name))
    health.record(
        account.name,
        "ready",
        "登录完成，页面就绪，出口IP已记录",
        current_ip=address,
        login_ip=address,
        proxy_fingerprint=record.proxy_fingerprint,
    )
    if settings.cloud and not account.proxy:
        log.warning("logged in from a datacenter IP without proxy; sends may be risk-controlled")
    if notify_success:
        notifier.push("登录成功", f"账号 {account.name} 已登录，出口IP {address}", dedupe=False)
    return record


def _wait_for_scan(
    chat: ChatPage,
    login_page: LoginPage,
    mode: LoginMode,
    publisher: QrPublisher,
    notifier: Notifier,
    timeout_s: float,
) -> None:
    """Poll until the chat page is ready, refreshing and publishing the QR meanwhile."""
    _tolerant(chat, login_page.open_panel)
    print("[login] 等待扫码 …")
    deadline = time.monotonic() + timeout_s
    warned_verify = False
    while time.monotonic() < deadline:
        state = _tolerant(chat, lambda: chat.state(wait_s=0))
        if state is PageState.READY:
            return
        if state is PageState.VERIFY and not warned_verify:
            warned_verify = True
            notifier.push(
                "登录需要验证", "请在浏览器中完成验证（服务器上请改用 --mode vnc）", dedupe=False
            )
        _tolerant(chat, login_page.refresh_if_expired)
        if mode is LoginMode.QR:
            png = _tolerant(chat, login_page.qr_png)
            if png:
                publisher.publish(png)
        chat.page.wait_for_timeout(int(POLL_SECONDS * 1000))
    raise LoginError(f"{timeout_s:.0f} 秒内未完成扫码登录")


def _tolerant(chat: ChatPage, action: Callable[[], T]) -> T | None:
    """Run a page action that may race with navigation; a closed window is fatal."""
    try:
        return action()
    except Exception as exc:
        if chat.page.is_closed():
            raise LoginError("浏览器窗口已关闭，登录中止") from exc
        log.debug("login page busy: %s", exc)
        return None


@contextmanager
def _noop() -> Iterator[None]:
    yield
