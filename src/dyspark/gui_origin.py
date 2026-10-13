"""Strict HTTP origin parsing shared by proxy adaptation and CSRF checks."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

Origin = tuple[str, str, int]


def parse_origin(value: str) -> Origin | None:
    if not value or len(value) > 512 or not value.isascii() or any(c.isspace() for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
            or parts.netloc.endswith(":")
        ):
            return None
        authority = (
            r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?"
            if parts.netloc.startswith("[")
            else r"[a-zA-Z0-9._-]+(?::[0-9]+)?"
        )
        if not re.fullmatch(authority, parts.netloc):
            return None
        host = parts.hostname.lower()
        if ":" in host:
            host = ipaddress.IPv6Address(host).compressed
            if "%" in host:
                return None
        elif not re.fullmatch(r"[a-z0-9._-]+", host):
            return None
        port = parts.port if parts.port is not None else 443 if parts.scheme == "https" else 80
        if not 0 < port < 65536:
            return None
        return parts.scheme, host, port
    except ValueError:
        return None


def format_origin(origin: Origin | None) -> str:
    if origin is None:
        return "<invalid>"
    scheme, host, port = origin
    authority = f"[{host}]" if ":" in host else host
    return f"{scheme}://{authority}:{port}"
