"""Plain failure reasons shared by the panel and notification text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RULES = json.loads(
    (Path(__file__).parent / "gui_static/delivery-failure.json").read_text(encoding="utf-8")
)


def failure_info(
    outcome: str, reason: str = "", *, held: bool = False, retry_exhausted: bool = False
) -> dict[str, Any]:
    text = str(reason or "")
    lower = text.lower()
    rule = next(
        (
            r
            for r in RULES
            if (outcome in ("risk", "logged_out") and outcome in r.get("outcomes", []))
        ),
        None,
    ) or next(
        (
            r
            for r in RULES
            if outcome in r.get("outcomes", [])
            or any(token in lower for token in r.get("tokens", []))
            or (r.get("all_tokens") and all(token in lower for token in r["all_tokens"]))
        ),
        {},
    )
    guidance = rule.get("guidance", "查看运行详情中的具体错误。")
    if held or outcome in ("sending", "unconfirmed"):
        guidance += "已暂停重复发送，防止消息重复。"
    if retry_exhausted:
        guidance = "已达到当天尝试上限，请先解决失败原因。"
    return {
        "label": "失败",
        "reason": rule.get("reason") or text or "发送未完成",
        "guidance": guidance,
        "action": rule.get("action", "logs"),
        "retry_allowed": not held and not retry_exhausted and outcome in ("failed", "friend_error"),
    }
