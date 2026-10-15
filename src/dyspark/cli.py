"""Argument parsing and dispatch for ``spark``; handlers live in ``commands``.

Exit codes: 0 ok / nothing to do, 1 partial failure or error, 2 login needed or
egress IP changed, 3 risk control, 64 configuration error, 75 another dyspark
process holds the data directory (login/check only; ``run`` skips quietly),
73 the data directory cannot be created or written, 69 the optional MCP or
GUI dependencies are missing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dyspark import __version__
from dyspark.commands.accounts import cmd_account_control, cmd_check, cmd_friends, cmd_login
from dyspark.commands.run import cmd_run
from dyspark.config import Settings, SettingsError, read_settings
from dyspark.jobs import JobReporter
from dyspark.locking import LockBusyError
from dyspark.logs import attach_service_log, brief_exc, harden_console, setup_logging
from dyspark.notify import Notifier
from dyspark.store import (
    DataDir,
    RunBusyError,
)
from dyspark.update_state import UpdateError
from dyspark.update_state import activity as update_activity

log = logging.getLogger("dyspark")

EXIT_CONFIG = 64
EXIT_BUSY = 75
EXIT_CANTCREAT = 73
EXIT_UNAVAILABLE = 69


def make_parser() -> argparse.ArgumentParser:
    # -v 只在此定义一次：主解析器和每个子命令通过 parents 共用，顶层/子命令两个位置都能用。
    # default=SUPPRESS 让「未出现该参数的一方」不写入 namespace，互相不覆盖。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="控制台输出调试日志"
    )
    parser = argparse.ArgumentParser(
        prog="spark", parents=[common], description="抖音续火花：定时一次性运行"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("SPARK_CONFIG", "config.toml")),
        help="config.toml 路径（默认 $SPARK_CONFIG 或当前目录的 config.toml）",
    )
    parser.add_argument(
        "--data", type=Path, default=None, help="数据目录（默认 config 同级 data/）"
    )
    parser.add_argument("--version", action="version", version=__version__)
    # Set by the MCP server for background jobs: progress/results go to <dir>/result.json.
    parser.add_argument("--job-dir", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    login = sub.add_parser("login", parents=[common], help="登录一个账号（扫码或短信）")
    login.add_argument("account")
    login.add_argument(
        "--mode",
        choices=["window", "qr", "vnc", "web"],
        default=None,
        help="window=本机窗口（本地默认）, qr=推送二维码（云端默认）, vnc=临时 noVNC, "
        "web=一次性登录网页（二维码/短信验证码）",
    )
    login.add_argument("--internal-relay", action="store_true", help=argparse.SUPPRESS)
    login.add_argument("--new-account", action="store_true", help=argparse.SUPPRESS)
    login.add_argument(
        "--proxy-ref",
        default=None,
        help="仅配合 --new-account：代理名称（在 .env 中定义，如 MAIN_PROXY）",
    )
    login.add_argument(
        "--timeout", type=float, default=None, help="等待登录的秒数（默认 login.timeout_minutes）"
    )

    run = sub.add_parser("run", parents=[common], help="发送今天的消息（定时触发或手动调用）")
    run.add_argument("--account", action="append", dest="accounts", help="只运行指定账号，可重复")
    run.add_argument("--dry-run", action="store_true", help="只定位好友，绝不发送")
    run.add_argument("--now", action="store_true", help="跳过随机启动延迟")
    run.add_argument("--trigger", default="cli", help=argparse.SUPPRESS)

    check = sub.add_parser("check", parents=[common], help="检查出口 IP 和登录状态，不发送")
    check.add_argument("--account", action="append", dest="accounts")

    for command, alias, help_text in (
        ("disable", "pause", "停用一个账号，阻止后续发送（不开浏览器）"),
        ("enable", "resume", "启用一个账号参与每日发送"),
    ):
        control = sub.add_parser(command, aliases=[alias], parents=[common], help=help_text)
        control.add_argument("account")

    sub.add_parser("status", parents=[common], help="查看登录记录和今日发送记录（不开浏览器）")
    friends = sub.add_parser(
        "friends",
        parents=[common],
        help="从抖音私信页拉取会话列表（需要已登录；默认只读，不改配置）",
    )
    friends.add_argument("--account", action="append", dest="accounts", help="只查指定账号，可重复")
    friends.add_argument(
        "--sparks-only",
        action="store_true",
        help="只输出带火花天数的好友",
    )
    friends.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 输出（便于 MCP / 脚本）",
    )
    friends.add_argument(
        "--save",
        action="store_true",
        help="把拉到的名字写入该账号 config.toml 的 friends（覆盖原名单）",
    )
    sub.add_parser(
        "mcp", parents=[common], help="以 stdio 方式运行 MCP 服务器（由 AI 客户端按需启动）"
    )
    gui = sub.add_parser(
        "gui", parents=[common], help="打开本机网页控制面板（账号 / 计划 / 状态 / 登录 / 运行）"
    )
    gui.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    gui.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    gui.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="启动后自动打开浏览器（默认不打开）",
    )
    sub.add_parser(
        "schedule",
        parents=[common],
        help="显示计划时间与项目常驻服务启动说明（spark gui）",
    )
    doctor = sub.add_parser("doctor", parents=[common], help="检查环境依赖、浏览器、配置和目录权限")
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    doctor.add_argument("--feature", choices=["cli", "gui", "mcp"], default="cli")
    password = sub.add_parser("password", parents=[common], help="管理面板密码")
    password_sub = password.add_subparsers(dest="password_action", required=True)
    reset = password_sub.add_parser("reset", parents=[common], help="停止面板服务后重置密码")
    reset.add_argument("--random", action="store_true", help="生成随机密码并在终端显示一次")
    return parser


def _now(settings: Settings) -> datetime:
    return datetime.now(settings.zone)


def cmd_status(settings: Settings) -> int:
    from dyspark.overview import format_status, snapshot

    print(format_status(snapshot(settings)))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from dyspark.mcp_server import serve
    except ImportError as exc:
        print(
            f'缺少 MCP 依赖（{exc.name or exc}）。请安装：pip install "dyspark[mcp]"',
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    # stdout carries the protocol: logs must go to stderr only.
    setup_logging(None, args.verbose, None, stream=sys.stderr)
    try:
        settings = read_settings(args.config.expanduser().resolve(), args.data)
    except SettingsError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    # AI clients often discard stderr; keep a persistent copy for post-mortems.
    attach_service_log(settings.data_dir, "mcp", settings.zone)
    if not settings.mcp_enabled:
        print(
            "MCP 已关闭（config.toml [mcp] enabled = false）。"
            "请在面板「设置」页开启，或手动把 enabled 改为 true。",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    serve(args.config.expanduser().resolve(), args.data)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from dyspark.gui import serve_gui
    except ImportError as exc:
        print(
            f'缺少 GUI 依赖（{exc.name or exc}）。请安装：pip install "dyspark[gui]"',
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    config = args.config.expanduser().resolve()
    # Validate config early so the browser does not open on a broken setup.
    try:
        settings = read_settings(config, args.data)
    except SettingsError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    # The panel is meant to run unattended (Task Scheduler, containers); keep logs on disk.
    setup_logging(None, args.verbose, settings.zone, stream=sys.stderr)
    attach_service_log(settings.data_dir, "gui", settings.zone)
    from dyspark.gui_auth import AuthError

    try:
        serve_gui(
            config,
            args.data,
            host=args.host,
            port=args.port,
            open_browser=args.open,
        )
    except AuthError as exc:
        print(f"面板认证配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except LockBusyError as exc:
        print(f"面板无法启动：{exc}", file=sys.stderr)
        return EXIT_BUSY
    except OSError as exc:
        print(f"面板无法启动：{brief_exc(exc)}", file=sys.stderr)
        return EXIT_CANTCREAT
    return 0


def cmd_schedule(settings: Settings, config_path: Path) -> int:
    from dyspark.schedule import describe, print_schedule

    print(print_schedule(describe(settings, config_path)))
    return 0


def _check_account_args(settings: Settings, args: argparse.Namespace) -> None:
    if args.command == "login" and getattr(args, "new_account", False):
        from dyspark.config import validate_account_name

        validate_account_name(args.account)
        return
    if args.command == "login" and getattr(args, "proxy_ref", None):
        from dyspark.config import SettingsError

        raise SettingsError(
            "--proxy-ref 只能配合 --new-account 使用（已有账号请在账号设置中改代理）"
        )
    names = list(getattr(args, "accounts", None) or [])
    if getattr(args, "account", None):
        names.append(args.account)
    for name in names:
        settings.account(name)  # raises SettingsError for unknown names


def main(argv: list[str] | None = None) -> int:
    harden_console()
    args = make_parser().parse_args(argv)
    args.verbose = getattr(args, "verbose", False)  # -v 在顶层和子命令都未出现时补默认值
    if args.command == "doctor":
        import json

        from dyspark.diagnostics import format_diagnostics, inspect_environment

        report = inspect_environment(args.config, args.data, feature=args.feature)
        print(
            json.dumps(report, ensure_ascii=True, indent=2)
            if args.json
            else format_diagnostics(report)
        )
        return 0 if report["ok"] else 1
    if args.command == "mcp":
        return cmd_mcp(args)
    if args.command == "gui":
        return cmd_gui(args)
    try:
        settings = read_settings(args.config.expanduser().resolve(), args.data)
        _check_account_args(settings, args)
    except SettingsError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.command == "status":
        return cmd_status(settings)
    if args.command == "schedule":
        return cmd_schedule(settings, args.config.expanduser().resolve())
    try:
        with update_activity(settings.data_dir):
            return _execute(args, settings)
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BUSY


def _execute(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "password":
        from dyspark.commands.password import cmd_password_reset

        return cmd_password_reset(settings, random_password=args.random)
    try:
        data = DataDir(settings.data_dir)
        if args.command == "status":
            return cmd_status(settings)
        if args.command in {"pause", "resume", "enable", "disable"}:
            return cmd_account_control(args)
        run_dir = data.new_run_dir(_now(settings))
    except OSError as exc:
        print(f"数据目录不可用：{settings.data_dir}（{exc}）", file=sys.stderr)
        return EXIT_CANTCREAT
    output = sys.stderr if args.command == "friends" and args.as_json else sys.stdout
    setup_logging(run_dir, args.verbose, settings.zone, stream=output)
    started = time.monotonic()
    log.info(
        "dyspark %s %s (data: %s, run: %s)",
        __version__,
        args.command,
        settings.data_dir,
        run_dir.name,
    )
    reporter = JobReporter(args.job_dir, args.command, settings.zone)
    code = 1
    try:
        if args.command == "login":
            code = cmd_login(settings, args, reporter)
        elif args.command == "check":
            code = cmd_check(settings, args, run_dir, reporter)
        elif args.command == "friends":
            code = cmd_friends(
                settings,
                args,
                run_dir,
                reporter,
                config_path=args.config.expanduser().resolve(),
            )
        else:
            code = cmd_run(settings, args, run_dir, reporter)
        return code
    except RunBusyError as exc:
        log.error("%s，请稍后再试", exc)
        reporter.data = {**reporter.data, "busy": True, "message": str(exc)}
        code = EXIT_BUSY
        return code
    except Exception as exc:
        log.exception("unexpected error")
        reporter.data = {**reporter.data, "error": f"{type(exc).__name__}: {brief_exc(exc)}"}
        if args.command == "run":
            Notifier(settings.notify, state_path=settings.data_dir / "notify-state.json").push(
                "运行异常", f"{type(exc).__name__}: {exc}"
            )
        return 1
    finally:
        reporter.finish(code)
        log.info("finished in %.1fs (exit %d)", time.monotonic() - started, code)


if __name__ == "__main__":
    raise SystemExit(main())
