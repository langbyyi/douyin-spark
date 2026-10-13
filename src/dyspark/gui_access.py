"""HTTP access gate for every GUI route, including mounted static files."""

from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from dyspark.gui_auth import COOKIE_NAME, PanelAuth
from dyspark.gui_origin import format_origin, parse_origin

log = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset(
    {
        "/auth/login",
        "/static/style.css",
        "/static/login.js",
        "/static/icon.svg",
        "/static/icon.png",
        "/static/apple-touch-icon.png",
        "/static/favicon.ico",
    }
)
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class PanelAccessMiddleware:
    def __init__(self, app: ASGIApp, *, auth: PanelAuth, require_https: bool = False):
        self.app = app
        self.auth = auth
        self.require_https = require_https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                if "Content-Security-Policy" not in headers:
                    headers["Content-Security-Policy"] = POLICY
            await send(message)

        path = scope["path"]
        response = None
        if self.require_https and scope["scheme"] != "https":
            message = "公网面板需要 HTTPS，请通过 HTTPS 反向代理访问"
            response = (
                JSONResponse({"ok": False, "error": message}, status_code=426)
                if path.startswith("/api/") or request.method == "POST"
                else HTMLResponse(message, status_code=426)
            )
        elif path not in PUBLIC_PATHS and not await run_in_threadpool(
            self.auth.valid, request.cookies.get(COOKIE_NAME)
        ):
            response = (
                RedirectResponse("/auth/login", status_code=303)
                if path == "/" and request.method in {"GET", "HEAD"}
                else JSONResponse({"ok": False, "error": "请先登录面板"}, status_code=401)
            )
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            origins = request.headers.getlist("origin")
            origin = parse_origin(origins[0]) if len(origins) == 1 else None
            expected = parse_origin(f"{scope['scheme']}://{request.headers.get('host', '')}")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if expected is None or (origins and origin != expected) or cross_site:
                log.warning(
                    "origin_rejected reason=%s expected=%s received=%s "
                    "(check proxy Host, X-Forwarded-Proto, X-Forwarded-Port and trusted sources)",
                    "cross_site" if cross_site else "origin_mismatch",
                    format_origin(expected),
                    format_origin(origin),
                )
                response = JSONResponse({"ok": False, "error": "不允许跨站操作"}, status_code=403)
        if response is not None:
            await response(scope, receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)
