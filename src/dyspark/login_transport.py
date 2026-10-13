"""Bounded requests to a job's local login listener, never its public URL."""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from dyspark.config import LoginOptions

READ_ROUTES = frozenset({"state", "qr.png", "screen.png"})
WRITE_ROUTES = frozenset({"sms/send", "sms/verify", "qr/refresh", "use-qr", "cancel"})
IMAGE_ROUTES = frozenset(route for route in READ_ROUTES if route.endswith(".png"))
MAX_RESPONSE = 2 * 1024 * 1024


class LoginTransportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoginResponse:
    status: int
    content: bytes
    content_type: str


def request_login(
    options: LoginOptions,
    info: dict[str, Any],
    method: str,
    tail: str,
    payload: dict[str, Any] | None = None,
) -> LoginResponse:
    if info.get("state") != "running":
        raise LoginTransportError("登录会话已结束，请重新发起登录")
    if not (
        (method == "GET" and tail in READ_ROUTES) or (method == "POST" and tail in WRITE_ROUTES)
    ):
        raise LoginTransportError("不支持的登录操作")
    try:
        url = urlsplit((info.get("session") or {}).get("url") or "")
        path = url.path
        match = re.search(r"/login/[A-Za-z0-9_-]{32,128}$", path)
        if match is None:
            raise LoginTransportError("登录验证页面尚未就绪")
        if "--internal-relay" in (info.get("args") or []):
            if (
                match.group() != path
                or url.scheme != "http"
                or url.hostname != "127.0.0.1"
                or not url.port
            ):
                raise LoginTransportError("登录验证服务必须使用本机地址")
            host, port = "127.0.0.1", url.port
        else:
            bind = options.bind
            host = (
                "::1" if bind == "::" else "127.0.0.1" if bind in ("0.0.0.0", "localhost") else bind
            )
            port = options.port
        path = match.group()
    except ValueError as exc:
        if isinstance(exc, LoginTransportError):
            raise
        raise LoginTransportError("登录验证服务地址无效") from None
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        encoded = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
        if encoded is not None and len(encoded) > 4096:
            raise LoginTransportError("登录请求过大")
        connection.request(
            method, f"{path}/{tail}", body=encoded, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        limit = MAX_RESPONSE if tail.endswith(".png") else 4096
        content = response.read(limit + 1)
        if len(content) > limit:
            raise LoginTransportError("登录验证页面响应过大")
        content_type = response.getheader("Content-Type", "application/json")
        expected = (
            "image/png" if tail.endswith(".png") and response.status == 200 else "application/json"
        )
        actual_type = content_type.split(";", 1)[0].strip()
        if actual_type != expected and not (response.status == 404 and actual_type == "text/plain"):
            raise LoginTransportError("登录验证页面响应无效")
        return LoginResponse(response.status, content, content_type)
    except (OSError, http.client.HTTPException) as exc:
        raise LoginTransportError("无法连接登录验证页面，请检查会话是否已结束") from exc
    finally:
        connection.close()
