"""Douyin web chat automation on top of a Playwright page.

All element lookups go through :mod:`dyspark.selectors`; this module only holds
behaviour (waiting, scrolling, typing, capturing the send response).
"""

from __future__ import annotations

import logging
import random
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dyspark import receipt as rc
from dyspark import selectors as sel
from dyspark.account_profile import OwnProfile, OwnProfileCapture
from dyspark.avatars import validate_avatar_url
from dyspark.names import normalize_name
from dyspark.send_request import SendIdentity, is_new_submission, request_identity
from dyspark.store import write_json_atomic

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page, Request, Response

log = logging.getLogger(__name__)

_JS_SEND_CONTEXT = """editor => {
  let fiber = editor[Object.keys(editor).find(k => k.startsWith('__reactFiber$'))];
  for (let depth = 0; fiber && depth < 32; depth++, fiber = fiber.return) {
    const conversation = fiber.memoizedProps?.curConversation;
    if (!conversation) continue;
    if (typeof conversation.id !== 'string' || !conversation.id
        || typeof conversation.getMessageList !== 'function') return null;
    const list = conversation.getMessageList(() => true);
    if (!Array.isArray(list)) return null;
    return { conversation: conversation.id, clock: Date.now(), messages: list.map(message => ({
      clientId: message.clientId, conversationId: message.conversationId,
      isFromMe: message.isFromMe, content: message.content,
      createdAt: message.createdAt instanceof Date ? message.createdAt.getTime() : null
    })) };
  }
  return null;
}"""

_JS_ARM_SUBMISSION = (
    """(editor, [key, expected]) => {
  const read = """
    + _JS_SEND_CONTEXT
    + """;
  const state = { before: null, error: null, handler: null };
  state.handler = event => {
    if (!event.isTrusted || event.key !== 'Enter' || event.shiftKey || event.isComposing
        || !editor.contains(event.target)) return;
    window.removeEventListener('keydown', state.handler, true);
    try { state.before = read(editor); } catch { state.before = null; }
    if (!state.before || state.before.conversation !== expected) {
      state.error = '当前会话标识已变化或无法读取，未发送';
      event.preventDefault(); event.stopImmediatePropagation();
    }
  };
  window[key] = state;
  window.addEventListener('keydown', state.handler, true);
}"""
)

_JS_READ_SUBMISSION = """key => {
  const state = window[key];
  return state ? {before: state.before, error: state.error} : null;
}"""

_JS_CLEAR_SUBMISSION = """key => {
  const state = window[key];
  if (state) window.removeEventListener('keydown', state.handler, true);
  delete window[key];
}"""


class PageState(StrEnum):
    READY = "ready"
    LOGGED_OUT = "logged_out"
    VERIFY = "verify"
    UNKNOWN = "unknown"


# One gate for every entry point (`spark check`, MCP check, the runner's live check),
# so the same page state records the same health status and reason everywhere.
PAGE_STATE_HEALTH = {
    PageState.LOGGED_OUT: ("logged_out", "登录已失效，请重新扫码"),
    PageState.VERIFY: ("blocked", "出现安全验证，请人工处理"),
    PageState.UNKNOWN: ("network_error", "聊天页未就绪，请检查网络和页面"),
}


def page_state_health(state: PageState) -> tuple[str, str]:
    """Map a non-ready page state to its ``(health_status, reason)`` pair."""
    return PAGE_STATE_HEALTH.get(state, PAGE_STATE_HEALTH[PageState.UNKNOWN])


class SendGuardError(RuntimeError):
    """The final recipient guard refused before Enter; nothing was submitted."""


@dataclass(frozen=True, slots=True)
class Conversation:
    name: str
    streak_days: int | None
    kind: str = "unknown"
    avatar_url: str = ""
    member_count: int | None = None


_JS_RECIPIENT_TEXT = """(el, excluded) => {
  const read = node => {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || '';
    if (node.nodeType !== Node.ELEMENT_NODE || node.matches(excluded)) return '';
    const style = getComputedStyle(node);
    if (style.display === 'none' || /^(hidden|collapse)$/.test(style.visibility)) return '';
    if (node.tagName === 'BR') return '\\n';
    const text = node.querySelector(excluded)
      ? [...node.childNodes].map(read).join('') : node.innerText || '';
    const inline = style.display.startsWith('inline') || style.display === 'contents';
    return inline ? text : '\\n' + text + '\\n';
  };
  return read(el);
}"""

_JS_ROWS = (
    """([rowSel, nameSel, streakSel, memberCountSel, groupAvatarSel, privateAvatarSel]) => {
  const recipientText = """
    + _JS_RECIPIENT_TEXT
    + """;
  const clean = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  return [...document.querySelectorAll(rowSel)].map(row => {
    const names = [...row.querySelectorAll(nameSel)].filter(n => !n.querySelector(nameSel));
    const counter = row.querySelector(streakSel);
    const digits = counter ? clean(counter.textContent).match(/\\d+/) : null;
    const groupAvatar = row.querySelector(groupAvatarSel);
    const privateAvatar = row.querySelector(privateAvatarSel);
    const avatar = groupAvatar || privateAvatar;
    const countText = row.querySelector(memberCountSel)?.innerText || '';
    const count = clean(countText).match(/^[(（]?([1-9]\\d*)[)）]?$/);
    return { name: names.length ? clean(recipientText(names[0], memberCountSel)) : '',
             kind: groupAvatar ? 'group' : privateAvatar ? 'private' : 'unknown',
             avatar_url: avatar ? avatar.currentSrc || avatar.src || '' : '',
             member_count: groupAvatar && count ? Number(count[1]) : null,
             streak: counter ? (digits ? Number(digits[0]) : 0) : null };
  });
}"""
)

_JS_SCROLL = """([scrollerSel, rowSel, toTop]) => {
  let box = document.querySelector(scrollerSel);
  if (!box) {
    const row = document.querySelector(rowSel);
    for (let el = row && row.parentElement; el && el !== document.body; el = el.parentElement) {
      const style = getComputedStyle(el);
      const scrollable = /(auto|scroll)/.test(style.overflowY);
      if (scrollable && el.scrollHeight > el.clientHeight) { box = el; break; }
    }
  }
  if (!box) return false;
  const before = box.scrollTop;
  box.scrollTop = toTop ? 0 : before + Math.max(200, box.clientHeight * 0.8);
  return toTop || box.scrollTop !== before;
}"""

JS_VISIBLE_TEXT = """([texts, excluded]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const value = walker.currentNode.nodeValue || '';
    if (!texts.some(t => value.includes(t))) continue;
    const el = walker.currentNode.parentElement;
    if (!el || excluded.some(s => el.closest(s))) continue;
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.height > 0) return value.trim().slice(0, 40);
  }
  return null;
}"""

_JS_SEARCH_RECIPIENT = """(action, wanted) => {
  const clean = s => (s || '').normalize('NFKC').replace(/\\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
  };
  const isAction = el => /^(发私信|发消息|聊天)$/.test(clean(el.textContent));
  for (let box = action.parentElement; box && box !== document.body; box = box.parentElement) {
    const elements = [...box.querySelectorAll('*')].filter(visible);
    const names = elements.filter(el => clean(el.textContent) === wanted &&
      ![...el.children].some(child => clean(child.textContent) === wanted));
    if (!names.length) continue;
    const actions = elements.filter(el => isAction(el) && ![...el.children].some(isAction));
    return names.length === 1 && actions.length === 1;
  }
  return false;
}"""


class ChatPage:
    """High-level actions on ``douyin.com/chat`` for one logged-in profile."""

    def __init__(
        self,
        page: Page,
        *,
        typing_delay_ms: tuple[int, int] = (60, 170),
        send_timeout_s: float = 15,
        artefact_dir: Path | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.page = page
        self._own_profile = OwnProfileCapture(page)
        self.typing_delay_ms = typing_delay_ms
        self.send_timeout_s = send_timeout_s
        self.artefact_dir = artefact_dir
        self.rng = rng or random.Random()
        self.last_open_error = ""

    # ----------------------------------------------------------------- navigation
    def own_profile(self) -> OwnProfile | None:
        return self._own_profile.read()

    def open(self, url: str = sel.CHAT_URL, *, wait_for_dom: bool = True) -> None:
        self.page.goto(
            url, wait_until="domcontentloaded" if wait_for_dom else "commit", timeout=60_000
        )
        if wait_for_dom:
            self.pause(2.0, 4.0)

    def pause(self, low: float, high: float) -> None:
        self.page.wait_for_timeout(int(self.rng.uniform(low, high) * 1000))

    def _first_visible(self, candidates: tuple[str, ...]) -> Locator | None:
        for candidate in candidates:
            locator = self.page.locator(candidate)
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        return None

    # ---------------------------------------------------------------- page state
    def verification_marker(self) -> str | None:
        if self._first_visible(sel.VERIFY_ELEMENTS):
            return "verification element"
        return self.page.evaluate(
            JS_VISIBLE_TEXT,
            [
                list(sel.VERIFY_TEXTS),
                [
                    sel.CONVERSATION_ROW,
                    sel.MESSAGE_BUBBLE,
                    *sel.COMPOSER,
                    sel.CONVERSATION_HEADER,
                    sel.CONVERSATION_HEADER_USER,
                ],
            ],
        )

    def state(self, wait_s: float = 30) -> PageState:
        """Poll until the page settles into ready / logged-out / verification.

        Errors while the page is navigating (e.g. "execution context was destroyed"
        right after a QR scan) count as "not settled yet" rather than failing.
        """
        deadline = time.monotonic() + wait_s
        while True:
            try:
                settled = self._probe_state()
            except Exception as exc:
                if self.page.is_closed():
                    raise
                log.debug("page busy while probing state: %s", exc)
                settled = None
            if settled is not None:
                return settled
            if time.monotonic() >= deadline:
                return PageState.UNKNOWN
            self.page.wait_for_timeout(500)

    def _probe_state(self) -> PageState | None:
        if self.verification_marker():
            return PageState.VERIFY
        if self._first_visible((sel.LOGIN_PANEL,)) or sel.visible_login_qr(self.page):
            return PageState.LOGGED_OUT
        if self._first_visible((sel.CONVERSATION_ROW,)) is not None:
            return PageState.READY
        return None

    # ------------------------------------------------------------- conversations
    def visible_conversations(self) -> list[Conversation]:
        rows = self.page.evaluate(
            _JS_ROWS,
            [
                sel.CONVERSATION_ROW,
                sel.CONVERSATION_NAME,
                sel.STREAK_COUNTER,
                sel.GROUP_MEMBER_COUNT,
                sel.GROUP_AVATAR,
                sel.PRIVATE_AVATAR,
            ],
        )
        return [
            Conversation(
                r["name"],
                r["streak"],
                r["kind"],
                validate_avatar_url(r["avatar_url"]),
                r["member_count"],
            )
            for r in rows
            if r["name"]
        ]

    def _scroll(self, to_top: bool = False) -> bool:
        moved = self.page.evaluate(
            _JS_SCROLL, [sel.CONVERSATION_SCROLLER, sel.CONVERSATION_ROW, to_top]
        )
        self.pause(0.6, 1.2)
        return bool(moved)

    def all_conversations(self, max_pages: int = 30) -> list[Conversation]:
        """Scroll through the (virtualised) list and collect every row once."""
        seen: dict[str, Conversation] = {}
        self._scroll(to_top=True)
        for _ in range(max_pages):
            for conv in self.visible_conversations():
                seen.setdefault(normalize_name(conv.name), conv)
            if not self._scroll():
                break
        return list(seen.values())

    def _click_row(self, name: str) -> bool:
        wanted = normalize_name(name)
        rows = self.page.locator(sel.CONVERSATION_ROW)
        matches = []
        for index in range(rows.count()):
            row = rows.nth(index)
            if not row.is_visible():
                continue
            names = row.locator(sel.CONVERSATION_NAME)
            titles = [
                self._recipient_name(names.nth(i))
                for i in range(names.count())
                if names.nth(i).is_visible()
                and names.nth(i).locator(sel.CONVERSATION_NAME).count() == 0
            ]
            if wanted in titles:
                if len(titles) != 1:
                    self._conversation_failure(name, "会话名称标识不唯一，未发送")
                    return False
                matches.append(row)
        if len(matches) > 1:
            self._conversation_failure(name, "存在多个同名或规范化名称相同的会话，未发送")
            return False
        if matches:
            matches[0].click()
            return True
        return False

    def _open_via_search(self, name: str) -> bool:
        box = self._first_visible(sel.SEARCH_BOX)
        if box is None:
            return False
        box.click()
        box.fill("")
        self.type_text(name)
        self.pause(1.5, 3.0)
        action = self.page.get_by_text(sel.SEARCH_HIT_ACTION)
        matches = []
        for index in range(action.count()):
            candidate = action.nth(index)
            if candidate.is_visible() and candidate.evaluate(
                _JS_SEARCH_RECIPIENT, normalize_name(name)
            ):
                matches.append(candidate)
        if len(matches) != 1:
            return False
        matches[0].click()
        return True

    @staticmethod
    def _recipient_name(anchor: Locator) -> str:
        text = anchor.evaluate(_JS_RECIPIENT_TEXT, sel.GROUP_MEMBER_COUNT)
        return normalize_name(text)

    def _selected_conversation_matches(self, name: str) -> bool:
        """Require a panel recipient anchor; a selected sidebar can precede the panel."""
        wanted = normalize_name(name)
        headers = self.page.locator(sel.CONVERSATION_HEADER)
        visible_headers = [
            headers.nth(i) for i in range(headers.count()) if headers.nth(i).is_visible()
        ]
        is_group = False
        selected = self.page.locator(sel.CONVERSATION_SELECTED)
        visible_selected = [
            selected.nth(i) for i in range(selected.count()) if selected.nth(i).is_visible()
        ]
        if visible_selected:
            if len(visible_selected) != 1:
                return False
            names = visible_selected[0].locator(sel.CONVERSATION_NAME)
            leaf_names = [
                self._recipient_name(names.nth(i))
                for i in range(names.count())
                if names.nth(i).is_visible()
                and names.nth(i).locator(sel.CONVERSATION_NAME).count() == 0
            ]
            if len(leaf_names) != 1 or leaf_names[0] != wanted:
                return False
            is_group = visible_selected[0].locator(sel.GROUP_AVATAR).count() > 0

        def matches_title(anchor: Locator) -> bool:
            title = self._recipient_name(anchor)
            if title == wanted:
                return True
            # The live group header appends an unlabelled count to the exact row name.
            if not is_group or not re.fullmatch(re.escape(wanted) + r"\s*\([1-9]\d*\)", title):
                return False
            # The sidebar can update before the panel; an old private nickname may
            # itself end in "(3)". Require the panel's own visible group avatar.
            return anchor.evaluate(
                """(el, [headerSel, groupSel, privateSel]) => {
                  const header = el.closest(headerSel);
                  if (!header || header.querySelector(privateSel)) return false;
                  const avatar = header.querySelector(groupSel);
                  if (!avatar) return false;
                  const style = getComputedStyle(avatar), box = avatar.getBoundingClientRect();
                  return style.visibility === 'visible' && box.width > 0 && box.height > 0;
                }""",
                [sel.CONVERSATION_HEADER_USER, sel.GROUP_AVATAR, sel.PRIVATE_AVATAR],
            )

        if visible_headers and (len(visible_headers) != 1 or not matches_title(visible_headers[0])):
            return False
        if visible_headers:
            return True

        headings = self.page.get_by_role("heading")
        matching = [
            headings.nth(i)
            for i in range(headings.count())
            if headings.nth(i).is_visible()
            and not headings.nth(i).evaluate(
                "(el, excluded) => !!el.closest(excluded)",
                f"{sel.CONVERSATION_ROW}, {sel.MESSAGE_BUBBLE}",
            )
            and matches_title(headings.nth(i))
        ]
        return len(matching) == 1

    def open_conversation(self, name: str, max_pages: int = 15) -> bool:
        """Select a private chat or group by its exact display name; return success."""
        self.last_open_error = ""
        self._scroll(to_top=True)
        found = self._click_row(name)
        if self.last_open_error:
            return False
        pages = 0
        while not found and pages < max_pages and self._scroll():
            found = self._click_row(name)
            if self.last_open_error:
                return False
            pages += 1
        if not found:
            log.info("%s not in conversation list, trying search", name)
            found = self._open_via_search(name)
        if not found:
            self._conversation_failure(name, "列表和搜索中未找到唯一匹配的会话")
            return False
        self.pause(1.0, 2.0)
        deadline = time.monotonic() + self.send_timeout_s
        while True:
            matches = self._selected_conversation_matches(name)
            if matches and self.composer() is not None:
                return True
            if time.monotonic() >= deadline:
                reason = (
                    "已确认收件人，但找不到消息输入框，未发送"
                    if matches
                    else "已点击匹配会话，但无法确认当前收件人，未发送"
                )
                self._conversation_failure(name, reason)
                return False
            self.page.wait_for_timeout(100)

    def _conversation_failure(self, name: str, reason: str) -> None:
        self.last_open_error = reason
        log.warning("[%s] %s", name, reason)
        if self.artefact_dir is None:
            return
        try:
            evidence = self.page.evaluate(
                """wanted => {
              const clean = s => (s || '').normalize('NFKC').replace(/\\s+/g,' ').trim();
              const visible = el => {const r=el.getBoundingClientRect();
                return r.width>0&&r.height>0;};
              const attrs = el => ({tag:el.tagName,role:el.getAttribute('role'),
                class:el.getAttribute('class'),e2e:el.getAttribute('data-e2e'),
                selected:el.getAttribute('aria-selected'),state:el.getAttribute('data-state')});
              const parents = el => {const chain=[];
                for(let i=0;el&&i<5;i++,el=el.parentElement) chain.push(attrs(el));return chain;};
              return {
                target_nodes:[...document.querySelectorAll('body *')].filter(el=>visible(el)
                  &&clean(el.textContent)===wanted&&!el.querySelector('input,textarea')
                  &&![...el.children].some(c=>clean(c.textContent)===wanted)).slice(0,12).map(parents),
                editors:[...document.querySelectorAll('[contenteditable="true"],textarea,[role="textbox"]')]
                  .filter(visible).slice(0,10).map(parents)
              };
            }""",
                normalize_name(name),
            )
            for label, selector in (
                ("headers", sel.CONVERSATION_HEADER),
                ("selected_rows", sel.CONVERSATION_SELECTED),
            ):
                anchors = self.page.locator(selector)
                evidence[label] = [
                    {
                        "class": anchors.nth(i).get_attribute("class"),
                        "text": (
                            (anchors.nth(i).text_content() or "")[:80]
                            if label == "headers"
                            else None
                        ),
                        "names": anchors.nth(i)
                        .locator(sel.CONVERSATION_NAME)
                        .evaluate_all(
                            "els => els.map(el => ({class:el.className,text:el.innerText}))"
                        ),
                    }
                    for i in range(anchors.count())
                    if anchors.nth(i).is_visible()
                ]
            evidence.update(target=name, reason=reason)
            stem = "conversation-" + str(time.time_ns())
            write_json_atomic(self.artefact_dir / f"{stem}.json", evidence)
            self.screenshot(self.artefact_dir / f"{stem}.png")
        except Exception as exc:
            log.debug("conversation diagnostics unavailable: %s", exc)

    # ------------------------------------------------------------------- sending
    def composer(self) -> Locator | None:
        return self._first_visible(sel.COMPOSER)

    def type_text(self, text: str) -> None:
        low, high = self.typing_delay_ms
        for char in text:
            self.page.keyboard.type(char)
            self.page.wait_for_timeout(self.rng.randint(low, high))

    def _failed_markers(self) -> int:
        return self.page.get_by_text(sel.SEND_FAILED_TEXT, exact=False).count()

    def send(
        self,
        text: str,
        *,
        before_submit: Callable[[], None] | None = None,
        friend: str | None = None,
    ) -> rc.Receipt:
        """Type ``text`` into the open chat, press Enter and judge the send response."""
        if friend is not None and not self._selected_conversation_matches(friend):
            raise SendGuardError("收件人已变化或无法确认，未发送，请人工核对")
        editor = self.composer()
        if editor is None:
            return rc.Receipt(rc.Outcome.FAILED, "composer not found")
        editor.click()
        self.page.keyboard.press("ControlOrMeta+A")
        self.page.keyboard.press("Backspace")
        for number, line in enumerate(text.split("\n")):
            if number:
                self.page.keyboard.press("Shift+Enter")
            self.type_text(line)
        self.pause(0.4, 1.0)

        if friend is not None and not self._selected_conversation_matches(friend):
            raise SendGuardError("收件人已变化或无法确认，未发送，请人工核对")
        initial = self._send_context(editor)
        if initial is None:
            return rc.Receipt(rc.Outcome.FAILED, "无法读取当前会话标识，未发送")
        if before_submit is not None:
            before_submit()
        # Network checks can take time; confirm the selected target again afterwards.
        if friend is not None and not self._selected_conversation_matches(friend):
            raise SendGuardError("收件人已变化或无法确认，未发送，请人工核对")

        before = self._send_context(editor)
        if before is None or before["conversation"] != initial["conversation"]:
            raise SendGuardError("当前会话标识已变化或无法读取，未发送")

        failed_before = self._failed_markers()
        submitted_request: Request | None = None
        captured_response: Response | None = None
        identity: SendIdentity | None = None
        request_count = 0
        submitting = False
        event_key = "__dyspark_submit_" + secrets.token_hex(16)

        def capture_request(request: Request) -> None:
            nonlocal submitted_request, identity, request_count
            if not submitting or not _is_send_request(request):
                return
            request_count += 1
            try:
                parsed = request_identity(request.post_data_buffer)
            except Exception:
                parsed = None
            if (
                request_count == 1
                and parsed is not None
                and parsed.conversation_id == before["conversation"]
                and parsed.text == text
            ):
                submitted_request, identity = request, parsed

        def matches_response(response: Response) -> bool:
            return (
                submitted_request is not None
                and response.request == submitted_request
                and _is_send_response(response)
            )

        def capture_response(response: Response) -> None:
            nonlocal captured_response
            if matches_response(response):
                captured_response = response

        editor.evaluate(_JS_ARM_SUBMISSION, [event_key, before["conversation"]])
        self.page.on("request", capture_request)
        self.page.on("response", capture_response)
        try:
            try:
                timeout_ms = self.send_timeout_s * 1000
                with self.page.expect_response(matches_response, timeout=timeout_ms) as info:
                    submitting = True
                    self.page.keyboard.press("Enter")
                    gate = self.page.evaluate(_JS_READ_SUBMISSION, event_key)
                    if not gate or gate.get("error") or not gate.get("before"):
                        raise SendGuardError("无法核实发送请求的本次按键事件")
                result = self._judge(info.value)
            except Exception as exc:  # playwright timeout and transport errors
                log.warning("no send response captured: %s", exc)
                transport_failure = submitted_request.failure if submitted_request else None
                reason = transport_failure or str(exc).split("\n", 1)[0][:240]
                result = rc.Receipt(rc.Outcome.UNCONFIRMED, f"no send response: {reason}")
            self.pause(2.0, 3.0)
            # A delayed receipt for this exact request may arrive during settling.
            # Observing it never presses Send again or accepts another request's receipt.
            if result.outcome is rc.Outcome.UNCONFIRMED and captured_response is not None:
                try:
                    result = self._judge(captured_response)
                except Exception as exc:
                    log.debug("late send receipt unavailable: %s", exc)
            if request_count > 1:
                return rc.unconfirmed("多个发送请求，无法核实发送请求的回执归属")
            if not request_count and result.ok:
                return rc.unconfirmed("no send response: no matching submission")
            gate = self.page.evaluate(_JS_READ_SUBMISSION, event_key)
            event_before = gate.get("before") if gate else None
            if request_count and (
                identity is None
                or not event_before
                or not is_new_submission(identity, event_before, self._send_context(editor), text)
            ):
                return rc.unconfirmed("无法核实发送请求与本次新消息的关联")
        finally:
            self.page.remove_listener("request", capture_request)
            self.page.remove_listener("response", capture_response)
            try:
                self.page.evaluate(_JS_CLEAR_SUBMISSION, event_key)
            except Exception:
                log.debug("submission observer unavailable during cleanup", exc_info=True)
        retryable = (rc.Outcome.OK, rc.Outcome.UNCONFIRMED)
        if self._failed_markers() > failed_before and result.outcome in retryable:
            # A global page marker may belong to another message. Contradictory
            # or missing receipts must retain the ledger hold, never grant a resend.
            return rc.Receipt(
                rc.Outcome.UNCONFIRMED,
                f"{result.reason}; page shows 发送失败，回执待核对，禁止自动重发",
                result.status,
                result.message_id,
            )
        return result

    @staticmethod
    def _send_context(editor: Locator) -> dict[str, Any] | None:
        try:
            value = editor.evaluate(_JS_SEND_CONTEXT)
            if (
                isinstance(value, dict)
                and isinstance(value.get("conversation"), str)
                and type(value.get("clock")) in (int, float)
                and isinstance(value.get("messages"), list)
                and all(isinstance(item, dict) for item in value["messages"])
            ):
                return value
        except Exception:
            log.debug("send identity unavailable", exc_info=True)
        return None

    def _judge(self, response: Response) -> rc.Receipt:
        body = response.body()
        if self.artefact_dir is not None:
            stamp = time.strftime("%H%M%S")
            try:
                (self.artefact_dir / f"send-{stamp}-{response.status}.bin").write_bytes(body)
            except OSError as exc:  # a full disk must not turn a real receipt into "unknown"
                log.warning("could not save the raw send response: %s", exc)
        return rc.from_response(response.status, body)

    def screenshot(self, path: Path) -> None:
        try:
            self.page.screenshot(path=str(path), full_page=False)
        except Exception as exc:
            log.warning("screenshot failed: %s", exc)


def _is_send_response(response: Response) -> bool:
    return _is_send_request(response.request)


def _is_send_request(request: Request) -> bool:
    url = urlsplit(request.url)
    return (
        url.scheme == "https"
        and url.hostname == "imapi.douyin.com"
        and url.path == "/v1/message/send"
        and request.method == "POST"
    )
