"""Adapter that exposes a real browser profile through the runner's session protocol."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from dyspark import egress
from dyspark.account_profile import OwnProfile
from dyspark.browser import open_context
from dyspark.chat import ChatPage, Conversation, PageState
from dyspark.config import Account, Settings
from dyspark.receipt import Receipt

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext


class LiveSession:
    def __init__(
        self, settings: Settings, account: Account, context: BrowserContext, run_dir: Path
    ) -> None:
        self.settings = settings
        self.account = account
        self.context = context
        self.run_dir = run_dir
        page = context.pages[0] if context.pages else context.new_page()
        self.chat = ChatPage(
            page,
            typing_delay_ms=settings.timing.typing_delay_ms,
            send_timeout_s=settings.timing.send_timeout_seconds,
            artefact_dir=run_dir if settings.save_raw_responses else None,
        )

    def egress_ip(self) -> str:
        return egress.lookup(self.context, self.settings.ip_echo_urls)

    def page_state(self) -> PageState:
        self.chat.open()
        return self.chat.state()

    def current_page_state(self) -> PageState:
        """Probe the selected conversation without navigating away from it."""
        return self.chat.state()

    def own_profile(self) -> OwnProfile | None:
        return self.chat.own_profile()

    def list_conversations(self, max_pages: int = 30) -> list[Conversation]:
        """Scroll the DM list and return every conversation currently loadable."""
        return self.chat.all_conversations(max_pages=max_pages)

    def open_friend(self, name: str) -> bool:
        return self.chat.open_conversation(name)

    @property
    def last_open_error(self) -> str:
        return self.chat.last_open_error

    def send(self, text: str) -> Receipt:
        return self.chat.send(text)

    def send_guarded(self, text: str, *, before_submit: Callable[[], None], friend: str) -> Receipt:
        """Recheck the target and runner safety guards after typing, before Enter."""
        return self.chat.send(text, before_submit=before_submit, friend=friend)

    def snapshot(self, label: str) -> None:
        name = f"{self.account.name}-{label}-{time.strftime('%H%M%S')}.png"
        self.chat.screenshot(self.run_dir / name)


@contextmanager
def live_session(settings: Settings, run_dir: Path, account: Account) -> Iterator[LiveSession]:
    with open_context(settings, account, headless=settings.browser.headless) as context:
        yield LiveSession(settings, account, context, run_dir)
