"""Optional conversation display metadata; never used to select recipients."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from dyspark.avatars import validate_avatar_url
from dyspark.names import normalize_name
from dyspark.store import write_json_atomic


def conversation_metadata(rows: object) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    valid = []
    for row in rows:
        if isinstance(row, str):
            row = {"name": row}
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"].strip():
            valid.append(row)
    counts = Counter(normalize_name(row["name"]) for row in valid)
    result = {}
    for row in valid:
        name = row["name"]
        # A name collision cannot identify a conversation, even with an exact spelling.
        if counts[normalize_name(name)] != 1:
            continue
        kind = row.get("kind")
        count = row.get("member_count")
        days = row.get("streak_days")
        result[name] = {
            "name": name,
            "kind": kind if kind in ("group", "private") else "unknown",
            "avatar_url": validate_avatar_url(row.get("avatar_url")),
            "member_count": count if type(count) is int and count > 0 else None,
            "streak_days": days if type(days) is int and days >= 0 else None,
        }
    return result


def load_conversations(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    rows = payload.get("conversations", []) if isinstance(payload, dict) else payload
    return conversation_metadata(rows)


def save_conversations(path: Path, rows: list[dict[str, Any]], updated_at: str) -> None:
    write_json_atomic(
        path,
        {"updated_at": updated_at, "conversations": list(conversation_metadata(rows).values())},
    )
