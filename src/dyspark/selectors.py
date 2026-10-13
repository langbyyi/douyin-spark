"""Every hook into the Douyin web pages lives here.

When the web client is redesigned, this should be the only file that needs edits.
Tuples are tried in order; the first visible match wins.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

CHAT_URL = "https://www.douyin.com/chat"

# Conversation list (left column)
CONVERSATION_ROW = '[data-e2e="conversation-item"]'
CONVERSATION_NAME = '[class*="ConversationItemtitle"]'
CONVERSATION_SCROLLER = '[class*="ConversationListwrapper"]'
CONVERSATION_SELECTED = (
    f'{CONVERSATION_ROW}[aria-selected="true"], '
    f'{CONVERSATION_ROW}[class*="ConversationItemcurConversation"]'
)
CONVERSATION_HEADER = '[class~="RightPanelHeadertitle"], [class*="RightPanelHeadertitle_"]'
CONVERSATION_HEADER_USER = '[class~="RightPanelHeaderuser"], [data-e2e="conversation-header"]'
# Observed web client avatar structures distinguish group and private conversations.
GROUP_AVATAR = 'img[class*="commonConversationIconnoDrag"], [data-e2e="group-avatar"] img'
PRIVATE_AVATAR = '[class*="commonIMAvataravatarContainer"] img, [data-e2e="private-avatar"] img'
# Only explicitly labelled metadata is excluded; numbers in a group name remain part of it.
GROUP_MEMBER_COUNT = (
    '[data-e2e="group-member-count"], [data-e2e="conversation-member-count"], '
    '[aria-label="群成员人数"]'
)
STREAK_COUNTER = '[class*="Streak"]'  # spark-day number rendered inside a row

# Search box and the "open chat" action on a search hit
SEARCH_BOX = ('input[placeholder="搜索"]', 'input[placeholder*="搜索"]')
SEARCH_HIT_ACTION = re.compile(r"^\s*(发私信|发消息|聊天)\s*$")

# Message composer
COMPOSER = (
    '[data-slate-editor="true"][contenteditable="true"]',
    '[data-e2e="msg-input"] [contenteditable="true"]',
    ".public-DraftEditor-content",
)
MESSAGE_BUBBLE = '[data-e2e="msg-item-content"]'
SEND_FAILED_TEXT = "发送失败"

# Login
LOGIN_PANEL = '[data-e2e="login-container"]'
LOGIN_ENTRY_TEXT = re.compile(r"^\s*登录\s*$")
QR_TAB_TEXT = re.compile(r"^\s*扫码登录\s*$")
QR_IMAGE = (
    'img[class*="qrcode"]',
    'canvas[class*="qrcode"]',
    'img[src^="data:image/png;base64"]',
    'img[src^="data:image/jpg;base64"]',
)
QR_EXPIRED = (
    '[class*="qrcode_expired"]',
    "text=二维码已失效",
    "text=二维码已过期",
    "text=二维码失效",
    "text=二维码过期",
)
QR_REFRESH_TEXT = re.compile(r"^\s*(点击)?刷新\s*$")
QR_SCANNED_TEXTS = ("扫码成功", "请在手机上确认", "需在手机上进行确认", "已扫码")


def visible_login_qr(page: Page) -> Locator | None:
    """A square login QR; chat avatars and received pictures are not login evidence."""
    outside_chat = f":not({CONVERSATION_ROW} *, {MESSAGE_BUBBLE} *)"
    for candidate in QR_IMAGE:
        images = page.locator(candidate + outside_chat)
        for index in range(min(images.count(), 20)):
            image = images.nth(index)
            box = image.bounding_box() if image.is_visible() else None
            if (
                box
                and box["height"] > 0
                and box["width"] >= 100
                and 0.8 <= box["width"] / box["height"] <= 1.25
            ):
                if 'class*="qrcode"' not in candidate and not image.evaluate(
                    """(image, [panel, row, bubble]) => {
                      if (image.closest(panel)) return true;
                      for (let el = image.parentElement; el && el !== document.body;
                           el = el.parentElement) {
                        if (el.querySelector(row + ', ' + bubble)) return false;
                        const labels = [...el.querySelectorAll('*')].filter(node =>
                          /^(扫码登录|使用抖音扫码登录)$/.test((node.textContent || '').trim()) &&
                          !node.children.length && node.getBoundingClientRect().width > 0);
                        if (labels.length) return true;
                      }
                      return false;
                    }""",
                    [LOGIN_PANEL, CONVERSATION_ROW, MESSAGE_BUBBLE],
                ):
                    continue
                return image
    return None


# Phone + SMS code login selectors. The SMS check after a QR scan was live-verified
# (2026-10-09); picking SMS as the primary login tab is best-effort and has not been
# verified against the live site.
SMS_TAB_TEXT = re.compile(r"^\s*(验证码登录|手机号登录|短信登录)\s*$")
PHONE_INPUT = (
    'input[type="tel"]:not([placeholder*="验证码"]):not([autocomplete="one-time-code"])',
    'input[placeholder*="手机号"]',
)
CODE_INPUT = ('input[placeholder*="验证码"]', 'input[autocomplete="one-time-code"]')
SEND_CODE_TEXT = re.compile(r"^\s*(获取验证码|发送验证码|重新发送|重新获取)\s*$")
SMS_RECEIVE_TEXT = re.compile(r"^\s*接收短信验证码\s*$")
# "/" is escaped: Playwright serialises these patterns as JavaScript /regex/ literals.
SUBMIT_TEXT = re.compile(r"^\s*(登录|登录\/注册|验证|确定|提交)\s*$")
AGREEMENT_CHECKBOX = 'input[type="checkbox"]'
SMS_SENT_TEXTS = ("短信已发送至", "验证码已发送", "已发送")
SMS_ERROR_TEXTS = ("验证码错误", "验证码不正确", "验证码已失效", "验证码已过期", "操作过于频繁")
SMS_VERIFY_TEXTS = ("请完成短信验证", "短信验证", "验证手机号", "手机验证", "短信已发送至")

# Security verification / captcha overlays
VERIFY_ELEMENTS = ("#captcha_container", 'iframe[src*="verifycenter"]', '[id*="captcha"]')
VERIFY_TEXTS = ("安全验证", "完成验证", "验证身份", "请完成下列验证", "拖动下方滑块")
