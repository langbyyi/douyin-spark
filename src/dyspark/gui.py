"""Local desktop control panel: ``spark gui``.

Serves a password-protected Chinese dashboard (Starlette + uvicorn). Bind stays
localhost unless you pass ``--host``; non-loopback listeners require HTTPS.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from html import escape
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from dyspark import __version__
from dyspark.config import load_env
from dyspark.gui_access import PanelAccessMiddleware
from dyspark.gui_auth import (
    COOKIE_NAME,
    PASSWORD_FILE,
    PASSWORD_SERVICE_LOCK,
    SESSION_SECONDS,
    AuthError,
    LoginLimited,
    PanelAuth,
    load_password,
    password_bytes,
)
from dyspark.gui_proxy import PanelProxyHeadersMiddleware
from dyspark.locking import file_lock
from dyspark.login_transport import READ_ROUTES, WRITE_ROUTES
from dyspark.panel import Backend, PanelError
from dyspark.relay_page import page_policy, render_page
from dyspark.update_state import UpdateError
from dyspark.update_state import operation as update_operation
from dyspark.updates import UpdateService

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "gui_static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_API_BODY_BYTES = 3 * 1024 * 1024  # Includes fully escaped maximum-size template/name lists.


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def create_app(
    backend: Backend,
    *,
    token: str,
    auth: PanelAuth | None = None,
    require_https: bool = False,
    password_path: Path | None = None,
) -> Starlette:
    """All routes require a password session; ``token`` also gates mutations."""
    backend.internal_login = True
    updates = UpdateService(backend.settings().data_dir, idle=backend.update_idle)
    password_path = password_path or backend.settings().data_dir / PASSWORD_FILE
    auth_env = load_env(backend.config_path)
    if auth is None:
        hashed, generated = load_password(password_path.parent, auth_env)
        auth = PanelAuth(hashed)
        if generated is not None:
            print(f"首次生成的面板密码（仅显示本次，请保存）：{generated}", flush=True)
            print(f"密码哈希已保存：{password_path}", flush=True)

    async def require_token(request: Request) -> None:
        got = request.headers.get("x-spark-token")
        if not got or not got.isascii() or not secrets.compare_digest(got, token):
            raise HTTPException(403, "缺少或错误的面板令牌")

    async def read_json(request: Request) -> dict[str, Any]:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "请求长度无效") from exc
            if size < 0:
                raise HTTPException(400, "请求长度无效")
            if size > MAX_API_BODY_BYTES:
                raise HTTPException(413, "请求内容过大")
        content = bytearray()
        try:
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_API_BODY_BYTES:
                    raise HTTPException(413, "请求内容过大")
                content.extend(chunk)
            data = json.loads(content)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "请求体必须是 JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        return data

    def number(value: Any, key: str, *, integer: bool = False) -> float | int:
        try:
            if isinstance(value, bool):
                raise ValueError
            result = int(value) if integer else float(value)
            if not math.isfinite(result):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(400, f"{key} 必须是有限{'整数' if integer else '数字'}") from exc
        return result

    def boolean(body: dict[str, Any], key: str) -> bool:
        value = body.get(key, False)
        if not isinstance(value, bool):
            raise HTTPException(400, f"{key} 必须是 true 或 false")
        return value

    async def call(fn, *args, **kwargs):
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except (PanelError, UpdateError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def ok(data: Any = None, **extra: Any) -> JSONResponse:
        payload: dict[str, Any] = {"ok": True}
        if data is not None:
            payload["data"] = data
        payload.update(extra)
        return JSONResponse(payload)

    async def index(_: Request) -> HTMLResponse:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        stamp = str(int(max(p.stat().st_mtime for p in STATIC.iterdir())))
        return HTMLResponse(
            html.replace("{{TOKEN}}", escape(token, quote=True))
            .replace("{{VERSION}}", __version__)
            .replace("{{STAMP}}", stamp)
        )

    async def panel_login(request: Request) -> Response:
        if request.method == "GET":
            return HTMLResponse((STATIC / "login.html").read_text(encoding="utf-8"))
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(400, "登录请求必须是 JSON")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > 4096:
                raise HTTPException(413, "登录请求过大")
            content.extend(chunk)
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "请求体必须是 JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        password = body.get("password")
        if not isinstance(password, str):
            raise HTTPException(400, "请输入面板密码")
        try:
            password_bytes(password)
            session = await run_in_threadpool(
                auth.login,
                password,
                request.client.host if request.client else "unknown",
                request.cookies.get(COOKIE_NAME),
            )
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        except LoginLimited as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        if session is None:
            return _json_error("密码错误", 401)
        response = ok()
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=require_https or request.url.scheme == "https",
            samesite="strict",
        )
        return response

    async def panel_logout(request: Request) -> Response:
        await require_token(request)
        await run_in_threadpool(auth.logout, request.cookies.get(COOKIE_NAME))
        response = ok()
        response.delete_cookie(
            COOKIE_NAME,
            httponly=True,
            secure=require_https or request.url.scheme == "https",
            samesite="strict",
        )
        return response

    def change_panel_password(*args):
        with update_operation(backend.settings().data_dir):
            return auth.change_password(*args)

    async def panel_password(request: Request) -> Response:
        await require_token(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "修改密码请求必须是 JSON")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > 4096:
                raise HTTPException(413, "修改密码请求过大")
            content.extend(chunk)
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "请求体必须是 JSON 对象") from exc
        if not isinstance(body, dict) or not all(
            isinstance(body.get(key), str)
            for key in ("current_password", "new_password", "confirm_password")
        ):
            raise HTTPException(400, "请填写当前密码、新密码和确认密码")
        if body["new_password"] != body["confirm_password"]:
            raise HTTPException(400, "两次输入的新密码不一致")
        try:
            changed = await run_in_threadpool(
                change_panel_password,
                body["current_password"],
                body["new_password"],
                request.cookies.get(COOKIE_NAME),
                request.client.host if request.client else "unknown",
                password_path,
            )
        except (AuthError, UpdateError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except LoginLimited as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        if not changed:
            raise HTTPException(400, "当前密码错误")
        response = ok({"message": "密码已修改，请使用新密码重新登录"})
        response.delete_cookie(
            COOKIE_NAME,
            httponly=True,
            secure=require_https or request.url.scheme == "https",
            samesite="strict",
        )
        return response

    async def api_meta(_: Request) -> JSONResponse:
        return ok(
            {"version": __version__, "commit": updates.commit, "config": str(backend.config_path)}
        )

    async def api_update(_: Request) -> JSONResponse:
        return ok(await call(updates.status))

    async def api_update_check(request: Request) -> JSONResponse:
        await require_token(request)
        return ok(await call(updates.check))

    async def api_update_apply(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        if set(body) != {"target"} or not isinstance(body["target"], str):
            raise HTTPException(400, "仅允许指定 target 提交编号")
        return ok(await call(updates.apply, body["target"]))

    async def api_status(request: Request) -> JSONResponse:
        return ok(await call(backend.status, request.query_params.get("account") or None))

    async def api_accounts(_: Request) -> JSONResponse:
        return ok(await call(backend.list_accounts))

    def account_endpoint(operation: str):
        async def endpoint(request: Request) -> JSONResponse:
            await require_token(request)
            return ok(await call(backend.account_control, operation, await read_json(request)))

        return endpoint

    async def api_schedule_control(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        if "enabled" not in body:
            raise HTTPException(400, "请指定 enabled")
        return ok(await call(backend.control_schedule, boolean(body, "enabled")))

    async def api_settings(request: Request) -> JSONResponse:
        if request.method == "POST":
            await require_token(request)
            body = await read_json(request)
            if "mcp_enabled" not in body:
                raise HTTPException(400, "请指定 mcp_enabled")
            return ok(await call(backend.control_mcp, boolean(body, "mcp_enabled")))
        return ok({"mcp_enabled": backend.settings().mcp_enabled})

    async def api_scheduler(_: Request) -> JSONResponse:
        return ok(await call(backend.scheduler_status))

    async def api_notifications(request: Request) -> JSONResponse:
        testing = request.method == "POST"
        if testing:
            await require_token(request)
        return ok(await call(backend.notifications, testing))

    async def api_notifications_save(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        key = body.get("serverchan_key")
        token = body.get("pushplus_token")
        for name, value in (("serverchan_key", key), ("pushplus_token", token)):
            if value is not None and not isinstance(value, str):
                raise HTTPException(400, f"{name} 必须是文字或 null")
        booleans = {
            name: body.get(name) for name in ("enabled", "clear_serverchan", "clear_pushplus")
        }
        for name, value in booleans.items():
            if value is not None and not isinstance(value, bool):
                raise HTTPException(400, f"{name} 必须是 true 或 false")
        return ok(
            await call(
                backend.save_notifications,
                key,
                token,
                booleans["enabled"],
                booleans["clear_serverchan"] or False,
                booleans["clear_pushplus"] or False,
            )
        )

    async def api_friends_add(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(await call(backend.add_friend, body.get("account"), body.get("name", "")))

    async def api_friends_remove(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(await call(backend.remove_friend, body.get("account"), body.get("name", "")))

    async def api_friends_set(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        names = body.get("friends")
        if not isinstance(names, list):
            raise HTTPException(400, "friends 必须是数组")
        return ok(await call(backend.replace_friends, body.get("account"), names))

    async def api_fetch(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(
            await call(
                backend.fetch_conversations,
                body.get("account"),
                boolean(body, "sparks_only"),
                number(body.get("wait_seconds", 5), "wait_seconds"),
            )
        )

    async def api_run(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(
            await call(
                backend.run,
                body.get("account"),
                boolean(body, "dry_run"),
                number(body.get("wait_seconds", 5), "wait_seconds"),
                trigger="gui",
            )
        )

    async def api_check(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(
            await call(
                backend.check,
                body.get("account"),
                number(body.get("wait_seconds", 5), "wait_seconds"),
            )
        )

    async def api_job(request: Request) -> JSONResponse:
        wait = number(request.query_params.get("wait_seconds") or 0, "wait_seconds")
        return ok(await call(backend.run_status, request.path_params.get("job_id"), wait))

    async def api_jobs_latest(request: Request) -> JSONResponse:
        wait = number(request.query_params.get("wait_seconds") or 0, "wait_seconds")
        return ok(await call(backend.run_status, None, wait))

    async def api_login_start(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        minutes = body.get("timeout_minutes")
        wait = body.get("wait_seconds")
        return ok(
            panel_login_payload(
                await call(
                    backend.login_start,
                    body.get("account"),
                    number(minutes, "timeout_minutes") if minutes is not None else None,
                    number(wait, "wait_seconds") if wait is not None else None,
                )
            )
        )

    async def api_account_connect(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        if set(body) - {"wait_seconds", "proxy"}:
            raise HTTPException(400, "添加账号直接扫码，账号信息在登录成功后读取")
        wait = body.get("wait_seconds")
        proxy_ref = body.get("proxy")
        if proxy_ref is not None and not isinstance(proxy_ref, str):
            raise HTTPException(400, "proxy 必须是代理地址或代理名称")
        return ok(
            panel_login_payload(
                await call(
                    backend.account_login_start,
                    number(wait, "wait_seconds") if wait is not None else None,
                    proxy_ref or None,
                )
            )
        )

    async def api_proxy_check(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        proxy = body.get("proxy")
        if not isinstance(proxy, str):
            raise HTTPException(400, "请先填写代理地址")
        return ok(await call(backend.check_proxy, proxy))

    async def api_login_status(request: Request) -> JSONResponse:
        wait = number(request.query_params.get("wait_seconds") or 0, "wait_seconds")
        return ok(
            panel_login_payload(
                await call(backend.login_status, request.path_params["session_id"], wait)
            )
        )

    def panel_login_payload(payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if result.get("url"):
            result["url"] = f"/account-login/{result['session_id']}"
        return result

    async def account_login_page(request: Request) -> Response:
        tail = request.path_params.get("tail", "").rstrip("/")
        method = request.method
        if (method == "GET" and tail not in READ_ROUTES | {""}) or (
            method == "POST" and tail not in WRITE_ROUTES
        ):
            raise HTTPException(404, "不支持的登录操作")
        body = None
        if method == "POST":
            await require_token(request)
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                raise HTTPException(415, "请求格式不支持")
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > 4096:
                    raise HTTPException(413, "登录请求过大")
                content.extend(chunk)
            try:
                body = json.loads(content)
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(400, "请求内容无效") from exc
            if not isinstance(body, dict):
                raise HTTPException(400, "请求内容无效")
        result = await call(
            backend.login_content, request.path_params["session_id"], method, tail or "state", body
        )
        if not tail and result.status == 200:
            state = json.loads(result.content)
            nonce = secrets.token_urlsafe(16)
            return HTMLResponse(
                render_page(state.get("account", ""), nonce, panel_token=token),
                headers={"Content-Security-Policy": page_policy(nonce)},
            )
        return Response(
            result.content, status_code=result.status, headers={"Content-Type": result.content_type}
        )

    async def api_login_cancel(request: Request) -> JSONResponse:
        await require_token(request)
        return ok(
            panel_login_payload(await call(backend.login_cancel, request.path_params["session_id"]))
        )

    async def api_login_refresh_qr(request: Request) -> JSONResponse:
        await require_token(request)
        return ok(await call(backend.login_refresh_qr, request.path_params["session_id"]))

    async def api_login_sms(request: Request) -> JSONResponse:
        await require_token(request)
        body = await read_json(request)
        return ok(
            await call(
                backend.login_sms,
                request.path_params["session_id"],
                request.path_params["action"],
                body,
            )
        )

    async def api_logs(request: Request) -> JSONResponse:
        lines = number(request.query_params.get("lines") or 80, "lines", integer=True)
        service = request.query_params.get("service") or None
        if service:
            return ok(await call(backend.service_log, service, lines))
        return ok(await call(backend.logs, request.query_params.get("run") or None, lines, True))

    async def api_schedule(request: Request) -> JSONResponse:
        if request.method == "POST":
            await require_token(request)
            body = await read_json(request)
            return ok(await call(backend.save_schedule, body.get("times")))
        return ok(await call(backend.schedule))

    async def api_media(request: Request) -> Response:
        path = await call(backend.media_file, request.query_params.get("path") or "")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def docs_mcp(_: Request) -> Response:
        for doc in (
            Path(__file__).resolve().parents[2] / "docs" / "mcp.md",
            Path.cwd() / "docs" / "mcp.md",
        ):
            if doc.is_file():
                return Response(
                    doc.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8"
                )
        return Response("见仓库 docs/mcp.md\n", media_type="text/plain; charset=utf-8")

    async def docs_updates(_: Request) -> Response:
        doc = Path(__file__).resolve().parents[2] / "docs" / "updates.md"
        content = doc.read_text(encoding="utf-8") if doc.is_file() else "见仓库 docs/updates.md\n"
        return Response(content, media_type="text/plain; charset=utf-8")

    async def on_error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            if request.url.path.startswith(("/api/", "/auth/")):
                return _json_error(detail, exc.status_code)
            return HTMLResponse(escape(detail), status_code=exc.status_code)
        log.exception("gui error")
        first = str(exc).splitlines()[0].strip() if str(exc) else ""
        brief = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
        if len(brief) > 180:
            brief = brief[:177] + "..."
        message = f"内部错误：{brief}"
        if request.url.path.startswith(("/api/", "/auth/")):
            return _json_error(message, 500)
        return HTMLResponse(escape(message), status_code=500)

    routes: list[Route | Mount] = [
        Route("/auth/login", panel_login, methods=["GET", "POST"]),
        Route("/auth/logout", panel_logout, methods=["POST"]),
        Route("/auth/password", panel_password, methods=["POST"]),
        Route("/", index),
        Route("/account-login/{session_id}", account_login_page, methods=["GET", "POST"]),
        Route(
            "/account-login/{session_id}/{tail:path}", account_login_page, methods=["GET", "POST"]
        ),
        Route("/api/meta", api_meta),
        Route("/api/update", api_update),
        Route("/api/update/check", api_update_check, methods=["POST"]),
        Route("/api/update/apply", api_update_apply, methods=["POST"]),
        Route("/api/status", api_status),
        Route("/api/accounts", api_accounts),
        Route("/api/accounts/add", account_endpoint("add"), methods=["POST"]),
        Route("/api/accounts/connect", api_account_connect, methods=["POST"]),
        Route("/api/accounts/update", account_endpoint("update"), methods=["POST"]),
        Route("/api/accounts/resume", account_endpoint("resume"), methods=["POST"]),
        Route("/api/accounts/enable", account_endpoint("enable"), methods=["POST"]),
        Route("/api/accounts/disable", account_endpoint("disable"), methods=["POST"]),
        Route("/api/accounts/delete", account_endpoint("delete"), methods=["POST"]),
        Route("/api/accounts/preview", account_endpoint("preview"), methods=["POST"]),
        Route("/api/proxy/check", api_proxy_check, methods=["POST"]),
        Route("/api/friends/add", api_friends_add, methods=["POST"]),
        Route("/api/friends/remove", api_friends_remove, methods=["POST"]),
        Route("/api/friends/set", api_friends_set, methods=["POST"]),
        Route("/api/friends/fetch", api_fetch, methods=["POST"]),
        Route("/api/run", api_run, methods=["POST"]),
        Route("/api/check", api_check, methods=["POST"]),
        Route("/api/jobs/latest", api_jobs_latest),
        Route("/api/jobs/{job_id}", api_job),
        Route("/api/login/start", api_login_start, methods=["POST"]),
        Route("/api/login/{session_id}", api_login_status),
        Route("/api/login/{session_id}/cancel", api_login_cancel, methods=["POST"]),
        Route("/api/login/{session_id}/qr/refresh", api_login_refresh_qr, methods=["POST"]),
        Route("/api/login/{session_id}/sms/{action}", api_login_sms, methods=["POST"]),
        Route("/api/logs", api_logs),
        Route("/api/schedule", api_schedule, methods=["GET", "POST"]),
        Route("/api/schedule/control", api_schedule_control, methods=["POST"]),
        Route("/api/settings", api_settings, methods=["GET", "POST"]),
        Route("/api/scheduler", api_scheduler),
        Route("/api/notifications", api_notifications),
        Route("/api/notifications/test", api_notifications, methods=["POST"]),
        Route("/api/notifications/save", api_notifications_save, methods=["POST"]),
        Route("/api/media", api_media),
        Route("/docs/mcp", docs_mcp),
        Route("/docs/updates", docs_updates),
    ]
    if STATIC.is_dir():
        routes.append(Mount("/static", StaticFiles(directory=str(STATIC)), name="static"))

    @asynccontextmanager
    async def lifespan(_app):
        from dyspark.scheduler import Scheduler

        backend.scheduler = Scheduler(backend)
        try:
            await run_in_threadpool(backend.start_service)
            updates.start()
            if getattr(backend, "automatic_allowed", True):
                await run_in_threadpool(backend.scheduler.start)
            yield
        finally:
            await run_in_threadpool(updates.stop)
            await run_in_threadpool(backend.stop_service)

    app = Starlette(
        lifespan=lifespan,
        routes=routes,
        middleware=[
            Middleware(
                PanelAccessMiddleware,
                auth=auth,
                require_https=require_https,
                health={
                    "X-Spark-Commit": updates.commit,
                    "X-Spark-Instance": updates.instance,
                    "X-Spark-Boot": updates.boot,
                },
            )
        ],
        exception_handlers={HTTPException: on_error, Exception: on_error},
    )
    app.state.updates = updates
    return app


def serve_gui(
    config_path: Path,
    data_dir: Path | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
    token: str | None = None,
) -> None:
    """Block on uvicorn until interrupted."""

    token = token or secrets.token_urlsafe(18)
    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    backend = Backend(config_path, data_dir)
    password_path = backend.settings().data_dir / PASSWORD_FILE
    with file_lock(password_path.parent / PASSWORD_SERVICE_LOCK):
        _serve_gui(backend, token, loopback, host, port, open_browser, password_path)


def _serve_gui(
    backend: Backend,
    token: str,
    loopback: bool,
    host: str,
    port: int,
    open_browser: bool,
    password_path: Path,
) -> None:
    import uvicorn

    app = create_app(backend, token=token, require_https=not loopback, password_path=password_path)
    updates = app.state.updates
    app = PanelProxyHeadersMiddleware(
        app, trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
    )
    url = f"http://{host}:{port}/"
    log.info("dyspark GUI %s → %s  (config=%s)", __version__, url, backend.config_path)
    log.info("控制面板：%s", url)
    log.info("请输入面板密码登录。按 Ctrl+C 停止服务。")
    if not loopback:
        log.info("公网监听已启用：请通过 HTTPS 反向代理访问，否则请求会被拒绝。")
    opener = None
    if open_browser:
        opener = threading.Timer(0.6, lambda: webbrowser.open(url))
        opener.daemon = True
        opener.start()
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=1,
            )
        )
        updates.on_shutdown = lambda: setattr(server, "should_exit", True)
        server.run()
    finally:
        if opener is not None:
            opener.cancel()
