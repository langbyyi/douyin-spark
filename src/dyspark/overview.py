"""Read-only views of the data directory: logins, today's results, runs and logs.

Shared by ``spark status``, the MCP tools and the GUI; nothing here starts a browser.
"""

from __future__ import annotations

import re
import socket
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from dyspark.account_health import HealthStore
from dyspark.avatars import validate_avatar_url
from dyspark.config import Account, Settings
from dyspark.delivery_failure import failure_info
from dyspark.jobs import tail
from dyspark.log_text import log_views
from dyspark.names import normalize_name
from dyspark.store import DataDir, Ledger, LoginRecord, marker_owner, mask_proxy, read_json

RUN_NAME = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


def _reservation_running(data: DataDir, entry: dict[str, Any]) -> bool:
    if entry.get("outcome") != "sending" or marker_owner(data.lock_file) is None:
        return False
    owner = read_json(data.lock_file / "owner.json", {})
    if not isinstance(owner, dict) or owner.get("host") != socket.gethostname():
        return False
    identity = owner.get("identity")
    return isinstance(identity, str) and bool(identity) and entry.get("run_identity") == identity


def account_view(data: DataDir, ledger: Ledger | None, account: Account) -> dict[str, Any]:
    login_error = None
    try:
        record = LoginRecord.load(data.login_file(account.name))
    except OSError:
        record = None
        login_error = "登录记录无法读取，请检查账号数据目录"
    health = HealthStore(data.root).view(account.name)
    entries = ledger.entries.get(account.name, {}) if ledger is not None else {}
    names_by_key = {}
    for friend in [*account.friends, *entries]:
        names_by_key.setdefault(normalize_name(friend), friend)
    names = list(names_by_key.values())
    blocked = ledger is None or ledger.blocked
    held = ledger.held(account.name, names) if not blocked else dict.fromkeys(names)
    friends = []
    for name in names:
        entry = ledger.entry(account.name, name) if ledger is not None else None
        if entry is None and not blocked:
            friends.append({"friend": name, "ok": None, "outcome": "pending", "attempts": 0})
        else:
            item = {**(entry or {}), "friend": name}
            if name in held:
                attempts = item.get("attempts")
                item["held"] = True
                item["retry_exhausted"] = (
                    not blocked
                    and type(attempts) is int
                    and attempts >= 3
                    and item.get("outcome") in ("failed", "friend_error")
                )
                if blocked:
                    item.update(
                        outcome="unconfirmed", reason="发送账本或历史记录异常，已停止自动发送"
                    )
            running = not blocked and _reservation_running(data, item)
            item["display_status"] = (
                "running" if running else "success" if item.get("ok") is True else "failed"
            )
            if item["display_status"] == "failed":
                item["failure"] = failure_info(
                    item.get("outcome", "failed"),
                    item.get("reason", ""),
                    held=bool(item.get("held")),
                    retry_exhausted=bool(item.get("retry_exhausted")),
                )
            friends.append(item)
    return {
        "name": account.name,
        "remark": account.remark,
        "nickname": record.nickname if record else "",
        "avatar_url": validate_avatar_url(record.avatar_url) if record else "",
        "display_name": account.remark or (record.nickname if record else "") or account.name,
        "enabled": account.enabled,
        "health": health,
        "login_error": login_error,
        "held_count": sum(1 for f in friends if f.get("held")),
        "proxy": mask_proxy(account.proxy) or None,
        "logged_in": record is not None,
        "login": asdict(record) if record else None,
        "friends": friends,
        "summary": {
            "ok": sum(1 for f in friends if f.get("ok") is True),
            "failed": sum(1 for f in friends if f.get("display_status") == "failed"),
            "running": sum(1 for f in friends if f.get("display_status") == "running"),
            "pending": sum(
                1 for f in friends if f.get("outcome") == "pending" and not f.get("held")
            ),
        },
    }


def snapshot(settings: Settings, account: str | None = None) -> dict[str, Any]:
    data = DataDir(settings.data_dir)
    today = datetime.now(settings.zone).date()
    ledger_error = None
    try:
        ledger = Ledger(data.ledger_file(today))
        if ledger.blocked:
            ledger_error = "发送账本或历史记录异常，已停止自动发送，请人工核对"
    except OSError:
        ledger = None
        ledger_error = "当天账本无法读取，已停止自动发送，请检查数据目录"
    accounts = [settings.account(account)] if account else list(settings.accounts)
    last_run_error = None
    try:
        last_run = read_json(data.last_run_file, None)
    except OSError:
        last_run = None
        last_run_error = "上次运行记录无法读取，请检查数据目录"
    return {
        "date": today.isoformat(),
        "timezone": settings.timezone,
        "ledger_blocked": ledger is None or ledger.blocked,
        "ledger_error": ledger_error,
        "accounts": [account_view(data, ledger, a) for a in accounts],
        "last_run": last_run if isinstance(last_run, dict) else None,
        "last_run_error": last_run_error,
        "busy": marker_owner(data.lock_file),
    }


def list_runs(data: DataDir, limit: int = 10) -> list[str]:
    try:
        folders = list((data.root / "runs").iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError("无法读取运行日志目录，请检查访问权限或磁盘状态") from exc
    runs = [
        p.name for p in folders if RUN_NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
    ]
    return sorted(runs, key=lambda name: (name[:15], int(name[16:] or 0)), reverse=True)[:limit]


SERVICE_LOGS = ("gui", "mcp")


def _log_tail(data: DataDir, path: Path, lines: int) -> list[str]:
    try:
        if not path.resolve().is_relative_to(data.root.resolve()):
            raise ValueError("日志路径不在数据目录内")
        return tail(path, lines, strict=True)
    except OSError as exc:
        raise ValueError("无法读取日志，请检查数据目录的访问权限或磁盘状态") from exc


def service_logs(data: DataDir, name: str, lines: int = 80) -> dict[str, Any]:
    """Tail of a resident service's log (``gui`` or ``mcp``) under ``logs/``.

    ``name`` is validated against a fixed allowlist, so this cannot read
    arbitrary files; rotated backups are listed by name only.
    """
    if name not in SERVICE_LOGS:
        raise ValueError(f"未知服务日志：{name!r}（可选 {'、'.join(SERVICE_LOGS)}）")
    folder = data.root / "logs"
    log_file = folder / f"{name}.log"
    files = sorted(p.name for p in folder.glob(f"{name}.log*")) if folder.is_dir() else []
    limit = max(1, min(lines, 500))
    recent = []
    for index in range(6):
        path = log_file if index == 0 else folder / f"{name}.log.{index}"
        recent = _log_tail(data, path, limit - len(recent)) + recent
        if len(recent) >= limit:
            break
    return {
        "service": name,
        "log_path": str(log_file),
        **log_views(recent[-limit:]),
        "rotated_files": files,
        "available": [n for n in SERVICE_LOGS if (folder / f"{n}.log").is_file()],
    }


def run_logs(data: DataDir, run: str | None = None, lines: int = 80) -> dict[str, Any]:
    """Tail of one run's log (the latest by default) and its screenshots."""
    runs = list_runs(data, 20)
    if run is None:
        run = next((r for r in runs if (data.root / "runs" / r / "run.log").is_file()), None)
    if run is None:
        return {"run": None, "lines": [], "screenshots": [], "recent_runs": runs}
    if not RUN_NAME.fullmatch(run):
        raise ValueError(f"运行目录名格式不对：{run!r}（例如 20261006-203000）")
    folder = data.root / "runs" / run
    if not folder.is_dir():
        raise ValueError(f"找不到运行目录 {run}")
    log_file = folder / "run.log"
    return {
        "run": run,
        "log_path": str(log_file),
        **log_views(_log_tail(data, log_file, max(1, min(lines, 500)))),
        "screenshots": [str(p) for p in sorted(folder.glob("*.png"))],
        "raw_responses": len(list(folder.glob("send-*.bin"))),
        "recent_runs": runs,
    }


def format_status(view: dict[str, Any]) -> str:
    """Human-readable ``spark status`` output."""
    lines = [f"今天：{view['date']}（{view['timezone']}）"]
    if view.get("ledger_blocked"):
        lines.append(f"⚠ {view.get('ledger_error') or '当天账本异常，已停止自动发送'}")
    for account in view["accounts"]:
        login = account["login"]
        if login:
            lines.append(
                f"== {account['name']}: 已登录 {login['logged_in_at']} 于 {login['host']} "
                f"({login['environment']}) 出口IP={login['egress_ip']} "
                f"代理={login['proxy'] or '-'}"
            )
        else:
            lines.append(f"== {account['name']}: 未登录（spark login {account['name']}）")
        health = account.get("health") or {}
        if account.get("login_error"):
            lines.append(f"   {account['login_error']}")
        if account.get("enabled") is False:
            lines.append(f"   未参与发送（开关关闭）：spark enable {account['name']}")
        elif health.get("paused"):
            lines.append(f"   已停发：{health.get('reason') or '需要重新核对'}")
            lines.append(f"   恢复发送：spark enable {account['name']}")
        untouched = []
        for friend in account["friends"]:
            if friend.get("display_status") == "running":
                lines.append(f"   … {friend['friend']}: 发送中，正在等待平台回执")
                continue
            if friend.get("held"):
                failure = friend.get("failure") or failure_info(
                    friend.get("outcome", "unconfirmed"),
                    friend.get("reason", ""),
                    held=True,
                    retry_exhausted=bool(friend.get("retry_exhausted")),
                )
                lines.append(
                    f"   ✘ {friend['friend']}: {failure['reason']} · {failure['guidance']}"
                )
                continue
            if friend.get("ok") is None:
                untouched.append(friend["friend"])
                continue
            mark = "✔" if friend["ok"] else "✘"
            lines.append(
                f"   {mark} {friend['friend']}: {friend.get('reason')} @ {friend.get('at')} "
                f"(第 {friend.get('attempts')} 次)"
            )
        if untouched:
            lines.append(f"   … 今天还没尝试：{'、'.join(untouched)}")
    last = view.get("last_run")
    if view.get("last_run_error"):
        lines.append(view["last_run_error"])
    if last:
        lines.append(
            f"上次运行：{last.get('finished_at')} 退出码 {last.get('exit_code')}"
            + ("（dry-run）" if last.get("dry_run") else "")
        )
    if view.get("busy"):
        lines.append(f"正在运行：{view['busy'].get('detail')}")
    return "\n".join(lines)
