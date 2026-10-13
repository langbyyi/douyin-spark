"""Read the browser's official self-profile response, never a friend's chat header."""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from dyspark.avatars import validate_avatar_url

log = logging.getLogger(__name__)
SELF_PROFILE_PATH = "/aweme/v1/web/user/profile/self/"


@dataclass(frozen=True, slots=True)
class OwnProfile:
    nickname: str
    user_id: str
    avatar_url: str = ""


def parse_own_profile(payload: Any) -> OwnProfile | None:
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    nickname = user.get("nickname")
    identity = user.get("sec_uid") or user.get("uid")
    if not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 128:
        return None
    if not isinstance(identity, (str, int)) or isinstance(identity, bool):
        return None
    identity = str(identity).strip()
    if (
        not identity
        or len(identity) > 256
        or any(unicodedata.category(ch) == "Cc" for ch in nickname + identity)
    ):
        return None
    avatar_url = ""
    for field in ("avatar_thumb", "avatar_medium", "avatar_larger"):
        avatar = user.get(field)
        urls = avatar.get("url_list") if isinstance(avatar, dict) else None
        if not isinstance(urls, list):
            continue
        avatar_url = next((url for raw in urls if (url := validate_avatar_url(raw))), "")
        if avatar_url:
            break
    return OwnProfile(nickname.strip(), identity, avatar_url)


class OwnProfileCapture:
    def __init__(self, page: Any) -> None:
        self.page = page
        self.profile: OwnProfile | None = None
        page.on("response", self._response)

    def _response(self, response: Any) -> None:
        try:
            parts = urlsplit(response.url)
            if (
                parts.scheme != "https"
                or parts.hostname not in {"www.douyin.com", "douyin.com"}
                or parts.path != SELF_PROFILE_PATH
            ):
                return
            profile = parse_own_profile(response.json())
            if profile:
                self.profile = profile
        except Exception:
            log.debug("self-profile response unavailable", exc_info=True)

    def read(self, wait_s: float = 3) -> OwnProfile | None:
        deadline = time.monotonic() + wait_s
        try:
            while self.profile is None and time.monotonic() < deadline:
                self.page.wait_for_timeout(100)
        except Exception:
            log.debug("self-profile wait unavailable", exc_info=True)
        return self.profile
