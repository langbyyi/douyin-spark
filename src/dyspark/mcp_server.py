"""MCP server over stdio: ``spark mcp``.

Wraps :class:`dyspark.panel.Backend`. The AI client starts this process on
demand; nothing stays resident. Quick tools read the data directory; slow
ones start a background ``spark`` job and return a job id to poll.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from dyspark import __version__
from dyspark.panel import FINISHED, STATUS_TEXT, Backend, PanelError, login_status_of

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
dyspark：通过已登录的浏览器会话向指定抖音好友或群聊发送消息。
- 使用 status 查询当日逐账号、逐好友结果，以及上次运行和登录状态。
- 未登录或出口 IP 变化时，使用 login_start 发起登录并向用户展示链接或二维码。
  使用 login_status 查询，等待 success / expired / failed / cancelled 终态，
  且 job_state 不再为 running 后再启动其他浏览器任务。
- run 执行后台发送；dry_run=true 仅定位好友，不提交消息。通过 run_status 查询结果。
- 已有账号重新登录保留原有启用状态；没有发送对象时不发送消息。
- 群聊以完整群名加入发送名单，只确认当前账号发送回执，不统计其他成员发言。
- 用户明确要求启用账号后调用 resume_account；要求已有登录记录。
  check 是可选检查，启用不要求预先检查，发送仍执行现场核验。
- pause_account 可在任务期间停用账号，阻止后续发送；已提交消息无法撤回。
- 浏览器任务共用运行锁；繁忙时拒绝启动新任务并返回当前任务信息。
- 使用 list_friends 查询名单，add_friend / remove_friend 修改 config.toml 并保留注释。
- 每日调度由 spark gui 服务执行，MCP 不承担调度；服务退出会取消所属后台任务。"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL_EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
LOCAL_REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
BROWSES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

# Re-export for tests that import these helpers from this module.
_login_status = login_status_of


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PanelError as exc:
        raise ToolError(str(exc)) from exc


def _as_tool_result(payload: dict[str, Any], *, image: bool) -> CallToolResult:
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=_describe(payload)),
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
    ]
    if image:
        picture = _image_for(payload)
        if picture is not None:
            content.append(picture)
    return CallToolResult(content=content, structured_content=payload)


def _describe(payload: dict[str, Any]) -> str:
    lines = [f"[{payload['status']}] {STATUS_TEXT.get(payload['status'], '')}"]
    if payload.get("message"):
        lines.append(str(payload["message"]))
    if payload.get("url") and payload["status"] not in FINISHED:
        lines.append(
            f"登录链接（一次性，{payload.get('expires_at') or ''} 前有效）：{payload['url']}"
        )
    if payload.get("novnc_url"):
        lines.append(f"noVNC：{payload['novnc_url']}")
    lines.append(f"session_id={payload['session_id']}")
    return "\n".join(lines)


def _image_for(payload: dict[str, Any]) -> ImageContent | None:
    status = payload["status"]
    path = payload.get("screen_path") if status == "verification_required" else None
    path = path or (payload.get("qr_path") if status in ("pending", "starting") else None)
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG") or len(data) > 2_000_000:
        return None
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png")


def build_server(config_path: Path, data_dir: Path | None = None) -> MCPServer:
    backend = Backend(config_path, data_dir)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            await anyio.to_thread.run_sync(backend.start_service)
            yield
        finally:
            await anyio.to_thread.run_sync(backend.stop_service)

    server = MCPServer("dyspark", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)

    @server.tool(
        description="列出 config.toml 中的所有账号：好友名单、是否已登录、"
        "登录时的出口 IP、代理（已脱敏）。",
        annotations=READ_ONLY,
    )
    def list_accounts() -> dict[str, Any]:
        return _call(backend.list_accounts)

    @server.tool(
        description="今天的发送情况：每个账号的登录状态、每个好友的结果（成功/失败/未尝试、原因、次数）、"
        "上次运行摘要、是否有任务正在运行、最近的后台任务。account 可选，用于限定查询账号。",
        annotations=READ_ONLY,
    )
    def status(account: str | None = None) -> dict[str, Any]:
        return _call(backend.status, account)

    @server.tool(
        description="检查登录状态和出口 IP 是否与登录时一致（会在后台打开浏览器，不发送消息）。"
        "会更新本地任务和健康记录。"
        "最多等待 wait_seconds 秒（≤55）；没结束则返回 job_id，用 run_status 继续查询。",
        annotations=BROWSES,
    )
    def check(account: str | None = None, wait_seconds: float = 50) -> dict[str, Any]:
        return _call(backend.check, account, wait_seconds)

    @server.tool(
        description="停用指定账号并保存配置，阻止后续发送；运行期间也可调用，不打开浏览器。",
        annotations=LOCAL_EDIT,
    )
    def pause_account(account: str) -> dict[str, Any]:
        return _call(backend.account_control, "update", {"account": account, "enabled": False})

    @server.tool(
        description="用户明确要求后启用指定账号。要求存在登录记录，不要求预先执行 check；"
        "清除缓存的健康结论，每次发送仍现场核验。存在运行任务或待核对任务时拒绝启用。"
        "不发送消息，不启用每日计划。",
        annotations=LOCAL_EDIT,
    )
    def resume_account(account: str) -> dict[str, Any]:
        return _call(backend.account_control, "resume", {"account": account})

    @server.tool(
        description="立即在后台运行一次发送（跳过随机启动延迟；今天已成功的好友会跳过）。"
        "account 为空表示全部账号；dry_run=true 只定位好友、绝不发送。遵守运行锁：已有任务时报错。"
        "返回 job_id 和已完成好友的结果；用 run_status 轮询直到 state=finished。",
        annotations=SENDS,
    )
    def run(
        account: str | None = None, dry_run: bool = False, wait_seconds: float = 20
    ) -> dict[str, Any]:
        return _call(backend.run, account, dry_run, wait_seconds)

    @server.tool(
        description="查询 run/check/friends 后台任务：state（running/finished/failed）、退出码、"
        "结果数据（发送结果或会话列表）。"
        "job_id 为空时查询最近一次；wait_seconds（≤55）可等待任务结束。",
        annotations=READ_ONLY,
    )
    def run_status(job_id: str | None = None, wait_seconds: float = 0) -> dict[str, Any]:
        return _call(backend.run_status, job_id, wait_seconds)

    @server.tool(
        description="查看运行日志（run/check/friends 的 run.log）末尾 lines 行及其截图路径，"
        '或常驻服务日志：service="gui"（面板）/"mcp" 时忽略 run。'
        "面板或 MCP 行为异常时先看服务日志。",
        annotations=READ_ONLY,
    )
    def logs(
        run: str | None = None,
        lines: int = 80,
        include_screenshots: bool = True,
        service: str | None = None,
    ) -> dict[str, Any]:
        if service:
            return _call(backend.service_log, service, lines)
        return _call(backend.logs, run, lines, include_screenshots)

    @server.tool(
        description="打开已登录账号的抖音私信页，滚动拉取会话列表（含火花天数）。"
        "不发送消息，会更新本地任务、会话和火花缓存。最多等待 wait_seconds 秒（≤55）；"
        "没结束则返回 job_id，用 run_status 继续查询。",
        annotations=BROWSES,
    )
    def fetch_conversations(
        account: str | None = None,
        sparks_only: bool = False,
        wait_seconds: float = 20,
    ) -> dict[str, Any]:
        return _call(backend.fetch_conversations, account, sparks_only, wait_seconds)

    @server.tool(
        description="列出发送对象名单（好友备注名、昵称或完整群名）。account 为空时列出所有账号。",
        annotations=READ_ONLY,
    )
    def list_friends(account: str | None = None) -> dict[str, Any]:
        return _call(backend.list_friends, account)

    @server.tool(
        description="把好友或群聊加入账号的名单（写入 config.toml，保留注释；"
        "重复或无效名字会被拒绝）。"
        "只有一个账号时 account 可省略。",
        annotations=LOCAL_EDIT,
    )
    def add_friend(name: str, account: str | None = None) -> dict[str, Any]:
        return _call(backend.add_friend, account, name)

    @server.tool(
        description="从账号的名单中移除好友或群聊（写入 config.toml，保留注释）。"
        "只有一个账号时 account 可省略。",
        annotations=LOCAL_REMOVE,
    )
    def remove_friend(name: str, account: str | None = None) -> dict[str, Any]:
        return _call(backend.remove_friend, account, name)

    @server.tool(
        description="在运行机器上发起登录（与定时任务相同的浏览器配置和代理），返回一次性登录链接、"
        "过期时间和二维码图片。用户可用抖音 App 扫码，或打开链接用手机号+短信验证码登录。"
        "会更新本地任务、浏览器资料和登录记录。timeout_minutes 可选，范围 1~60 分钟。"
        "之后用 login_status 轮询到 finished=true。已有该账号的登录会话时直接返回它；"
        "finalizing 表示仍在核对环境并保存，不能当作成功。",
        annotations=BROWSES,
    )
    def login_start(
        account: str | None = None, timeout_minutes: float | None = None
    ) -> CallToolResult:
        return _as_tool_result(_call(backend.login_start, account, timeout_minutes), image=True)

    @server.tool(
        description="查询登录会话状态：starting / pending / scanned / sms_required / "
        "verification_required / finalizing / success / expired / failed / cancelled。"
        "finished=true 表示登录流程结束；启动其他任务前还需等待 job_state 不再为 running。"
        "wait_seconds（≤55）可等待状态变化；include_image=true 附带当前二维码或验证截图。",
        annotations=READ_ONLY,
    )
    def login_status(
        session_id: str, wait_seconds: float = 0, include_image: bool = False
    ) -> CallToolResult:
        return _as_tool_result(
            _call(backend.login_status, session_id, wait_seconds), image=include_image
        )

    @server.tool(description="取消登录会话并关闭登录页和浏览器。", annotations=LOCAL_EDIT)
    def login_cancel(session_id: str) -> CallToolResult:
        return _as_tool_result(_call(backend.login_cancel, session_id), image=False)

    return server


def serve(config_path: Path, data_dir: Path | None = None) -> None:
    log.info("dyspark %s MCP server (config: %s)", __version__, config_path)
    build_server(config_path, data_dir).run("stdio")
