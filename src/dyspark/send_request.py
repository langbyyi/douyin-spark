"""Correlate a send response with a newly created local SDK message.

Field positions are verified against captured requests, not an official schema.
Unknown schemas or ambiguous identities must never authorize a success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dyspark.wire import Message, WireType


@dataclass(frozen=True)
class SendIdentity:
    conversation_id: str
    client_id: str
    text: str


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def content_text(content: str) -> str | None:
    try:
        obj = json.loads(content, object_pairs_hook=_unique_pairs)
        return obj["text"] if isinstance(obj, dict) and isinstance(obj.get("text"), str) else None
    except (ValueError, TypeError):
        return None


def _single(message: Message, number: int, kind: WireType) -> int | bytes:
    fields = message.fields.get(number, [])
    if len(fields) != 1 or fields[0].wire_type is not kind:
        raise ValueError("missing or ambiguous send field")
    return fields[0].value


def request_identity(raw: bytes | None) -> SendIdentity | None:
    if not raw or len(raw) > 1024 * 1024:
        return None
    try:
        envelope = Message(raw)
        if _single(envelope, 1, WireType.VARINT) != 100:
            return None
        body = Message(_single(envelope, 8, WireType.LENGTH))
        send = Message(_single(body, 100, WireType.LENGTH))
        conversation = _single(send, 1, WireType.LENGTH).decode("utf-8")
        text = content_text(_single(send, 4, WireType.LENGTH).decode("utf-8"))
        ids = []
        for field in send.fields.get(5, []):
            if field.wire_type is not WireType.LENGTH:
                return None
            ext = Message(field.value)
            key = _single(ext, 1, WireType.LENGTH).decode("utf-8")
            if key == "s:client_message_id":
                ids.append(_single(ext, 2, WireType.LENGTH).decode("utf-8"))
        if not conversation or len(ids) != 1 or not ids[0] or text is None:
            return None
        return SendIdentity(conversation, ids[0], text)
    except (ValueError, TypeError, AttributeError):
        return None


def is_new_submission(
    identity: SendIdentity, before: dict[str, Any], after: dict[str, Any] | None, text: str
) -> bool:
    if not after or before["conversation"] != after["conversation"]:
        return False
    if identity.conversation_id != before["conversation"] or identity.text != text:
        return False
    if identity.client_id in {item.get("clientId") for item in before["messages"]}:
        return False
    matching = [item for item in after["messages"] if item.get("clientId") == identity.client_id]
    if len(matching) != 1:
        return False
    item = matching[0]
    created = item.get("createdAt")
    return (
        item.get("conversationId") == identity.conversation_id
        and item.get("isFromMe") is True
        and content_text(item.get("content", "")) == text
        and type(created) in (int, float)
        and before["clock"] <= created <= after["clock"]
    )
