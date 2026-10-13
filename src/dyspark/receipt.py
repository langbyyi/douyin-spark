"""Interpretation of the IM "send message" HTTP response.

Facts used here (observed on the web client, not an official spec):

* endpoint: ``https://imapi.douyin.com/v1/message/send``; the body is protobuf,
  occasionally JSON when a gateway rejects the request (e.g. ``{"decision": "KICK"}``);
* response envelope: field 1 = command id (100 for send), 3 = result code
  (0 = success, 8 = not logged in), 4 = status text (``"OK"``), 6 = body;
* inside the body, field 100 carries the send result: 1 = server message id and,
  less certain, 3 = delivery status, 5 = check code, 6 = check message;
* delivery status values: 0 ok, 1 UserNotInConversation, 2 CheckConversationNotPass,
  3 CheckMessageNotPass, 4 CheckMessageNotPassButSelfVisible, 5 UserHasBeenBlock.

Unknown or unparsable bodies never count as success.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from dyspark.wire import Message, printable_strings

SEND_URL_PART = "imapi.douyin.com/v1/message/send"
SEND_COMMAND = 100
CODE_NOT_LOGGED_IN = 8

DELIVERY_STATUS = {
    0: "Succeeded",
    1: "UserNotInConversation",
    2: "CheckConversationNotPass",
    3: "CheckMessageNotPass",
    4: "CheckMessageNotPassButSelfVisible",
    5: "UserHasBeenBlock",
}
RISK_STATUSES = frozenset({2, 3, 4})
FRIEND_STATUSES = frozenset({1, 5})
PASSING_DECISIONS = frozenset({"", "PASS", "ALLOW", "ACCEPT"})

_DECISION_RE = re.compile(r'"decision"\s*:\s*"([A-Za-z_]*)"')


class Outcome(StrEnum):
    OK = "ok"
    RISK = "risk"  # risk control: KICK or a content/conversation check failed
    LOGGED_OUT = "logged_out"
    FRIEND_ERROR = "friend_error"  # problem with this friend only (blocked, left chat)
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"  # no send response observed

    @property
    def stops_account(self) -> bool:
        """Whether sending to further friends of the same account must stop."""
        return self in (Outcome.RISK, Outcome.LOGGED_OUT)


@dataclass(frozen=True, slots=True)
class Receipt:
    outcome: Outcome
    reason: str
    status: int | None = None
    message_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


def unconfirmed(reason: str = "no send response") -> Receipt:
    return Receipt(Outcome.UNCONFIRMED, reason)


def from_response(http_status: int, body: bytes) -> Receipt:
    """Classify one captured send response."""
    if http_status != 200:
        return unconfirmed(f"http {http_status}; delivery unknown")
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        return _from_json(stripped)
    return _from_protobuf(body)


def _decision_of(text: str) -> str | None:
    match = _DECISION_RE.search(text)
    if match:
        return match.group(1).upper()
    return "KICK" if text.strip().upper() == "KICK" else None


def _from_json(body: bytes) -> Receipt:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return unconfirmed("unparsable json body")
    if not isinstance(payload, dict):
        return unconfirmed("unexpected json body")
    decision = str(payload.get("decision") or "").upper()
    if decision not in PASSING_DECISIONS:
        return Receipt(Outcome.RISK, f"decision={decision}")
    code = payload.get("status_code", payload.get("statusCode"))
    if isinstance(code, int) and code != 0:
        return _from_status(code, "json")
    return unconfirmed("json body without a send result")


def _from_status(status: int, origin: str, message_id: str | None = None) -> Receipt:
    name = DELIVERY_STATUS.get(status, f"status{status}")
    if status == 0:
        return Receipt(Outcome.OK, f"{origin}:{name}", status, message_id)
    if status in RISK_STATUSES:
        return Receipt(Outcome.RISK, f"{origin}:{name}", status, message_id)
    if status in FRIEND_STATUSES:
        return Receipt(Outcome.FRIEND_ERROR, f"{origin}:{name}", status, message_id)
    return Receipt(Outcome.UNCONFIRMED, f"{origin}:{name}", status, message_id)


def _from_protobuf(body: bytes) -> Receipt:
    envelope = Message.try_parse(body)
    if envelope is None:
        return unconfirmed("body is neither json nor protobuf")

    for text in printable_strings(body):
        decision = _decision_of(text)
        if decision and decision not in PASSING_DECISIONS:
            return Receipt(Outcome.RISK, f"decision={decision}")

    command = envelope.int(1)
    if command not in (None, SEND_COMMAND):
        return unconfirmed(f"unexpected response command={command}")

    code = envelope.int(3)
    status_text = (envelope.text(4) or "").strip()
    if code == CODE_NOT_LOGGED_IN:
        return Receipt(Outcome.LOGGED_OUT, f"envelope code {code}")
    if code not in (0, None):
        return Receipt(Outcome.FAILED, f"envelope code {code} {status_text}".strip())

    body_msg = envelope.child(6)
    result = body_msg.child(SEND_COMMAND) if body_msg else None
    message_id = None
    if result is not None:
        server_id = result.int(1)
        message_id = str(server_id) if server_id else None
        check_code, check_text = result.int(5), result.text(6)
        if check_code:
            return Receipt(Outcome.RISK, f"check_code={check_code} {check_text or ''}".strip())
        delivery = result.int(3)
        if delivery is not None and delivery != 0:
            return _from_status(delivery, "pb", message_id)

    if code == 0 and status_text.upper() == "OK" and message_id:
        return Receipt(Outcome.OK, "pb:OK", 0, message_id)
    if result is not None and result.int(3) == 0 and code == 0 and status_text.upper() == "OK":
        return Receipt(Outcome.OK, "pb:OK (no message id)", 0)
    return unconfirmed(f"unrecognised envelope code={code} status={status_text!r}")
