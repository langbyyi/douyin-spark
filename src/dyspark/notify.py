"""Outbound notifications: Server酱 and PushPlus pushes."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from dyspark.config import NotifyOptions
from dyspark.store import read_json, write_json_atomic

log = logging.getLogger(__name__)

TITLE_LIMIT = 32
_SCTP_KEY = re.compile(r"^sctp(\d+)t", re.IGNORECASE)
PUSHPLUS_URL = "https://www.pushplus.plus/send"
# 同一标题在此窗口内不重复推送：定时失败重试间隔 30 分钟，避免一次故障刷屏配额。
REPEAT_SUPPRESS = timedelta(hours=1)


def serverchan_url(sendkey: str) -> str:
    """Server酱 Turbo keys use sctapi; Server酱³ keys (``sctp<uid>t...``) use push.ft07.com."""
    match = _SCTP_KEY.match(sendkey)
    if match:
        return f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    return f"https://sctapi.ftqq.com/{sendkey}.send"


class Notifier:
    def __init__(
        self,
        options: NotifyOptions,
        prefix: str = "抖音火花",
        state_path: Path | None = None,
    ) -> None:
        self.options = options
        self.prefix = prefix
        self.state_path = state_path

    def push(
        self, title: str, body: str = "", redact: tuple[str, ...] = (), *, dedupe: bool = True
    ) -> bool:
        """Send to every configured push channel; failures are logged, never raised.

        Gated by ``notify.enabled``: when off, nothing is sent and this returns False.
        With ``dedupe`` (the default) a repeat of the same title within
        :data:`REPEAT_SUPPRESS` is dropped, so scheduled retries do not burn quota;
        the panel's test button passes ``dedupe=False``. Returns True only when all
        configured channels confirmed delivery, and False when nothing is configured,
        pushing is disabled, or a repeat was suppressed.

        Strings in ``redact`` (one-time link tokens) are masked in the log line only.
        """
        if not self.options.enabled:
            log.debug("notify disabled, dropping: %s", title)
            return False
        if dedupe and self._suppressed(title):
            log.info("notify: %s suppressed (same title pushed recently)", title)
            return False
        full_title = f"{self.prefix}·{title}"[:TITLE_LIMIT]
        logged = body.replace("\n", " / ")
        for secret in redact:
            if secret:
                logged = logged.replace(secret, "<token>")
        log.info("notify: %s | %s", full_title, logged)
        outcomes: list[tuple[str, bool, str]] = []
        if self.options.serverchan_key:
            ok, detail = self._push_serverchan(full_title, body)
            outcomes.append(("Server酱", ok, detail))
        if self.options.pushplus_token:
            ok, detail = self._push_pushplus(full_title, body)
            outcomes.append(("PushPlus", ok, detail))
        if not outcomes:
            return False
        ok_all = all(ok for _, ok, _ in outcomes)
        self._record(title, ok_all, outcomes)
        return ok_all

    def _push_serverchan(self, title: str, body: str) -> tuple[bool, str]:
        data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
        url = serverchan_url(self.options.serverchan_key)
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as response:
                answer = response.read(4096)
        except Exception as exc:
            log.warning("server酱 push failed: %s", exc)
            return False, f"请求失败（{exc}）"
        ok, reason = _serverchan_accepted(answer)
        return (True, "") if ok else (False, f"平台未接受（{reason}）")

    def _push_pushplus(self, title: str, body: str) -> tuple[bool, str]:
        payload = json.dumps(
            {
                "token": self.options.pushplus_token,
                "title": title,
                "content": body,
                "template": "txt",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            PUSHPLUS_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answer = response.read(4096)
        except Exception as exc:
            log.warning("pushplus push failed: %s", exc)
            return False, f"请求失败（{exc}）"
        ok, reason = _pushplus_accepted(answer)
        return (True, "") if ok else (False, f"平台未接受（{reason}）")

    def _state(self) -> dict:
        if not self.state_path:
            return {}
        try:
            state = read_json(self.state_path, None)
        except OSError as exc:
            log.warning("notify state read failed: %s", exc)
            return {}
        return state if isinstance(state, dict) else {}

    def _suppressed(self, title: str) -> bool:
        recent = self._state().get("recent")
        if not isinstance(recent, dict):
            return False
        last = recent.get(title)
        if not isinstance(last, str):
            return False
        try:
            instant = datetime.fromisoformat(last)
        except ValueError:
            return False
        return datetime.now(instant.tzinfo) - instant < REPEAT_SUPPRESS

    def _record(self, title: str, ok: bool, outcomes: list[tuple[str, bool, str]]) -> None:
        """Persist the dedupe map and the latest outcome for the panel to show."""
        if not self.state_path:
            return
        now = datetime.now().astimezone()
        state = self._state()
        recent = state.get("recent")
        if not isinstance(recent, dict):
            recent = {}
        recent = {k: v for k, v in recent.items() if isinstance(k, str) and isinstance(v, str)}
        recent[title] = now.isoformat(timespec="seconds")
        cutoff = (now - timedelta(days=1)).isoformat(timespec="seconds")
        recent = {k: v for k, v in recent.items() if v >= cutoff}
        failures = [f"{name}：{detail}" for name, ok_, detail in outcomes if not ok_]
        state["recent"] = recent
        state["last"] = {
            "at": now.isoformat(timespec="seconds"),
            "title": title,
            "ok": ok,
            "detail": "；".join(failures),
        }
        try:
            write_json_atomic(self.state_path, state)
        except Exception as exc:
            log.warning("notify state write failed: %s", exc)


def _serverchan_accepted(answer: bytes) -> tuple[bool, str]:
    """Server酱 answers HTTP 200 even for errors; the JSON ``code`` tells the truth."""
    try:
        payload = json.loads(answer.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.warning("server酱 returned a non-JSON answer")
        return False, "返回非 JSON"
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == 0:
        return True, ""
    message = payload.get("message") if isinstance(payload, dict) else payload
    log.warning("server酱 rejected the push: code=%s %s", code, message)
    return False, f"code={code} {message}"


def _pushplus_accepted(answer: bytes) -> tuple[bool, str]:
    """pushplus answers HTTP 200 even for errors; the JSON ``code`` tells the truth."""
    try:
        payload = json.loads(answer.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.warning("pushplus returned a non-JSON answer")
        return False, "返回非 JSON"
    if isinstance(payload, dict) and payload.get("code") == 200:
        return True, ""
    log.warning("pushplus rejected the push: %s", payload)
    if isinstance(payload, dict):
        return False, json.dumps(payload, ensure_ascii=False)
    return False, "响应异常"
