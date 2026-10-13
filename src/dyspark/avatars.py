"""Validate remote avatar references without downloading image data."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit


def validate_avatar_url(raw: Any) -> str:
    """Return a safe HTTPS URL, or an empty string for invalid input."""
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 4096
        or "\\" in raw
        or any(ch.isspace() or unicodedata.category(ch) in {"Cc", "Cf"} for ch in raw)
    ):
        return ""
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            return ""
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            domain = host.encode("idna").decode("ascii").rstrip(".")
            if len(domain) > 253 or any(
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in domain.split(".")
            ):
                return ""
    except (ValueError, UnicodeError):
        return ""
    return raw
