"""Launching a persistent browser profile for one account."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dyspark import runtime
from dyspark.config import Account, Settings, proxy_credentials, validate_proxy
from dyspark.runtime import supports_window_position
from dyspark.store import DataDir, trim_profile_cache

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

log = logging.getLogger(__name__)

WINDOW_SIZE = (1280, 900)
OFFSCREEN_POSITION = (-2400, -2400)


class BrowserLaunchError(RuntimeError):
    """The browser could not be started (not installed, profile in use, bad proxy...)."""


def proxy_settings(proxy: str | None) -> dict[str, str] | None:
    """Translate ``scheme://user:pass@host:port`` into Playwright's proxy dict.

    Credentials may be percent-encoded in the URL (``p%40ss`` for ``p@ss``).
    """
    if not proxy:
        return None
    validate_proxy(proxy, "proxy")
    parts = urlsplit(proxy)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    server = f"{parts.scheme.lower()}://{host}" + (f":{parts.port}" if parts.port else "")
    settings = {"server": server}
    username, password = proxy_credentials(proxy)
    if username:
        settings["username"] = username
        settings["password"] = password
    return settings


def chrome_kwargs(
    settings: Settings, account: Account, *, on_screen: bool, headless: bool = False
) -> dict[str, Any]:
    """Keyword arguments for ``launch_persistent_context``."""
    width, height = WINDOW_SIZE
    args = [
        f"--window-size={width},{height}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-CN",
    ]
    if not headless and on_screen and not settings.cloud and supports_window_position():
        args.append("--window-position=80,60")
    elif (
        not headless
        and settings.browser.offscreen
        and not settings.cloud
        and supports_window_position()
    ):
        args.append("--window-position={},{}".format(*OFFSCREEN_POSITION))
    options: dict[str, Any] = {
        "headless": headless,
        "locale": "zh-CN",
        "timezone_id": settings.timezone,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    if headless:
        options["viewport"] = {"width": width, "height": height}
    else:
        options["no_viewport"] = True
    if settings.browser.executable_path:
        options["executable_path"] = str(
            Path(settings.browser.executable_path).expanduser().resolve()
        )
    elif settings.browser.channel != "chromium":
        options["channel"] = settings.browser.channel
        executable = runtime.browser_executable(settings.browser)
        if executable is not None:
            options["executable_path"] = str(executable)
    proxy = proxy_settings(account.proxy)
    if proxy:
        options["proxy"] = proxy
    return options


@contextmanager
def open_context(
    settings: Settings, account: Account, *, on_screen: bool = False, headless: bool = False
) -> Iterator[BrowserContext]:
    """Start the account's persistent profile; always close it (and trim caches) afterwards."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    data = DataDir(settings.data_dir)
    profile = data.profile_dir(account.name)
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                **chrome_kwargs(settings, account, on_screen=on_screen, headless=headless),
            )
        except PlaywrightError as exc:
            raise BrowserLaunchError(_launch_hint(str(exc), settings)) from exc
        try:
            yield context
        finally:
            try:
                context.close()
            except Exception as exc:  # browser already gone; nothing left to release
                log.debug("closing the browser failed: %s", exc)
    freed = trim_profile_cache(profile)
    if freed:
        log.debug("trimmed %.1f MB of browser cache", freed / 1e6)


def _launch_hint(message: str, settings: Settings) -> str:
    first = message.strip().splitlines()[0] if message.strip() else "unknown error"
    lowered = message.lower()
    if (
        "executable doesn't exist" in lowered
        or "is not found at" in lowered
        or "chromium distribution" in lowered
    ):
        return (
            f"找不到浏览器（channel={settings.browser.channel}）：请安装 Google Chrome，"
            f"或执行 python -m playwright install {settings.browser.channel}。原始错误：{first}"
        )
    if "processsingleton" in lowered or ("profile" in lowered and "in use" in lowered):
        return f"浏览器资料目录正被另一个 Chrome 进程使用，请先关闭它。原始错误：{first}"
    if "missing x server" in lowered or "$display" in lowered:
        return f"没有可用的显示器：Linux 上请用 xvfb-run 运行。原始错误：{first}"
    return f"浏览器启动失败：{first}"
