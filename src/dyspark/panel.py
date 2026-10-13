"""Shared control-plane backend for MCP and the desktop GUI.

Raises :class:`PanelError` (plain ``ValueError``) so neither surface needs the
other's framework. Slow actions start background ``spark`` jobs via
:mod:`dyspark.jobs` and return a job id to poll.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dyspark.accounts import discard_unregistered_profile, validate_proxy_reference
from dyspark.config import (
    Account,
    Settings,
    SettingsError,
    expand_env,
    load_env,
    read_settings,
    validate_proxy,
)
from dyspark.friends import FriendEditError, add_friend, remove_friend, set_friends
from dyspark.jobs import MAX_WAIT_SECONDS, Job, JobError, JobManager
from dyspark.login_transport import LoginResponse, LoginTransportError, request_login
from dyspark.messages import message_presets
from dyspark.overview import account_view, run_logs, service_logs, snapshot
from dyspark.store import DataDir, Ledger, SparkRecord, marker_owner, read_json
from dyspark.weblogin import Status

LOGIN_START_WAIT = 45.0
# Derived from the enum so a new login status cannot silently miss its label set.
FINISHED = tuple(s.value for s in Status if s.finished)
STATUS_TEXT = {
    "starting": "正在启动浏览器…",
    "pending": "等待扫码：请用抖音 App 扫描二维码，或打开链接用短信验证码登录",
    "scanned": "已扫码，请在手机上确认登录",
    "sms_required": "需要短信验证码：请在下方输入并提交",
    "verification_required": "抖音要求安全验证，请打开链接查看（滑块验证需使用 noVNC）",
    "finalizing": "已通过抖音验证，正在核对运行环境并保存登录…",
    "success": "登录成功",
    "expired": "登录链接已过期",
    "failed": "登录失败",
    "cancelled": "登录已取消",
}

# User-facing labels for background job kinds (GUI / MCP / CLI messages).
KIND_LABEL = {
    "run": "发送任务",
    "check": "检查任务",
    "login": "登录会话",
    "friends": "会话拉取任务",
}
POLL_JOB_KINDS = ("run", "check", "friends")


class PanelError(ValueError):
    """User-facing refusal from a panel / MCP / GUI action."""


def _clamp(seconds: float | None, default: float) -> float:
    try:
        if isinstance(seconds, bool):
            raise ValueError
        value = default if seconds is None else float(seconds)
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise PanelError("wait_seconds 必须是有限数字") from exc
    return max(0.0, min(value, MAX_WAIT_SECONDS))


def _today(settings: Settings) -> date:
    return datetime.now(settings.zone).date()


def _optional_account(name: str | None) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str):
        raise PanelError("account 必须是账号名文字或 null")
    return name.strip() or None


class Backend:
    """Tool logic shared by MCP and the GUI (unit-testable)."""

    def __init__(self, config_path: Path, data_dir: Path | None = None) -> None:
        self.config_path = config_path
        self.data_arg = data_dir
        self._managers: dict[Path, JobManager] = {}
        self._start_lock = threading.Lock()
        self._service_lock = threading.Lock()
        self.scheduler = None
        self._service_active = False
        self._service_stopping = False
        self.internal_login = False

    def start_service(self) -> None:
        with self._service_lock:
            self._start_service()

    def _start_service(self) -> None:
        with self._start_lock:
            if self._service_active:
                return
            if self._service_stopping:
                # Do not discard a failed cleanup's still-owned process scopes.
                for manager in self._managers.values():
                    try:
                        manager.shutdown()
                    except Exception as exc:
                        raise PanelError(f"服务重启前无法回收原有任务：{exc}") from exc
                self._managers.clear()
            self._service_stopping = False
            self._service_active = True
            for manager in self._managers.values():
                manager.enable_service()

    def stop_service(self) -> None:
        with self._service_lock:
            self._stop_service()

    def _stop_service(self) -> None:
        with self._start_lock:
            self._service_stopping = True
            self._service_active = False
            managers = list(self._managers.values())
        errors = []
        if self.scheduler is not None:
            try:
                self.scheduler.stop()
            except Exception as exc:
                errors.append(str(exc))
        for manager in managers:
            try:
                manager.shutdown()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise PanelError("服务停止时任务回收失败：" + "；".join(errors))

    def settings(self) -> Settings:
        try:
            return read_settings(self.config_path, self.data_arg)
        except SettingsError as exc:
            raise PanelError(f"配置错误：{exc}") from exc

    def jobs(self, settings: Settings) -> JobManager:
        root = settings.data_dir
        with self._start_lock:
            if root not in self._managers:
                manager = JobManager(DataDir(root), self.config_path, settings.zone)
                if self._service_active:
                    manager.enable_service()
                self._managers[root] = manager
            return self._managers[root]

    @staticmethod
    def account(settings: Settings, name: str | None) -> Account:
        name = _optional_account(name)
        if name is None:
            if len(settings.accounts) == 1:
                return settings.accounts[0]
            if not settings.accounts:
                raise PanelError("尚未添加账号，请先添加抖音账号")
            names = "、".join(a.name for a in settings.accounts)
            raise PanelError(f"有多个账号（{names}），请指定 account")
        try:
            return settings.account(name)
        except SettingsError as exc:
            raise PanelError(str(exc)) from exc

    def _job(self, settings: Settings, job_id: str, kind: str | None = None) -> Job:
        try:
            job = self.jobs(settings).get(job_id)
        except JobError as exc:
            raise PanelError(str(exc)) from exc
        if kind and job.kind != kind:
            label = KIND_LABEL.get(kind, kind)
            raise PanelError(f"{job_id} 不是{label}")
        return job

    def _ensure_idle(self, settings: Settings, jobs: JobManager) -> None:
        active = jobs.active()
        if active:
            job = active[0]
            if job.get("state") == "blocked" or job.get("blocked"):
                raise PanelError(
                    f"{job.get('message') or '任务状态待核对，已阻止启动'}（{job['job_id']}）"
                )
            label = KIND_LABEL.get(job["kind"], job["kind"])
            raise PanelError(
                f"已有任务在运行：{job['job_id']}（{label}）。"
                "请用 run_status / login_status 查看，或等待其结束"
            )
        owner = marker_owner(DataDir(settings.data_dir).lock_file)
        if owner:
            raise PanelError(
                f"另一个 dyspark 进程正在使用数据目录（{owner.get('detail')}），请稍后再试"
            )

    def _start(self, settings: Settings, kind: str, args: list[str]) -> Job:
        jobs = self.jobs(settings)
        with self._start_lock:
            if self._service_stopping:
                raise PanelError("项目服务正在停止，不能启动新任务")
            self._ensure_idle(settings, jobs)
            try:
                return jobs.start(kind, args)
            except JobError as exc:
                raise PanelError(str(exc)) from exc

    # ---------------------------------------------------------- read-only
    def list_accounts(self) -> dict[str, Any]:
        from dyspark.account_controls import AccountControls
        from dyspark.conversation_cache import load_conversations

        settings = self.settings()
        metadata = AccountControls(self.config_path, self.data_arg).metadata(settings)
        data = DataDir(settings.data_dir)
        ledger = Ledger(data.ledger_file(_today(settings)))
        accounts = []
        for account in settings.accounts:
            view = account_view(data, ledger, account)
            login = view["login"] or {}
            sparks = SparkRecord.load(data.sparks_file(account.name))
            accounts.append(
                {
                    "name": account.name,
                    "remark": view["remark"],
                    "nickname": view["nickname"],
                    "avatar_url": view["avatar_url"],
                    "display_name": view["display_name"],
                    "friends": list(account.friends),
                    "send_results": view["friends"],
                    "conversation_metadata": load_conversations(
                        data.account_dir(account.name) / "conversations.json"
                    ),
                    "proxy": view["proxy"],
                    "logged_in": view["logged_in"],
                    "egress_ip_at_login": login.get("egress_ip"),
                    "logged_in_at": login.get("logged_in_at"),
                    "spark_days": sparks.days if sparks else {},
                    "sparks_updated_at": sparks.updated_at if sparks else None,
                    **metadata[account.name],
                }
            )
        return {
            "config": str(self.config_path),
            "cloud": settings.cloud,
            "accounts": accounts,
            "global_templates": list(settings.messages.templates),
            "message_presets": message_presets(_today(settings)),
        }

    def status(self, account: str | None = None) -> dict[str, Any]:
        settings = self.settings()
        account = _optional_account(account)
        name = self.account(settings, account).name if account else None
        view = snapshot(settings, name)
        view["recent_jobs"] = self.jobs(settings).recent(5)
        view["scheduler"] = self.scheduler_status()
        return view

    def account_control(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        from dyspark.account_controls import AccountControls
        from dyspark.config_edit import ConfigEditError
        from dyspark.store import RunBusyError, exclusive_run

        if operation == "enable":
            operation = "resume"
        elif operation == "disable":
            operation = "update"
            payload = {**payload, "enabled": False}
        if operation == "delete" and (set(payload) != {"account"} or not payload.get("account")):
            raise PanelError("删除账号必须明确指定账号标识")

        control = AccountControls(self.config_path, self.data_arg)
        settings = self.settings()
        jobs = self.jobs(settings)
        try:
            if operation == "preview":
                if set(payload) - {"account", "friend", "templates"}:
                    raise PanelError("预览仅支持 account、friend 和 templates")
                target = self.account(settings, payload.get("account"))
                draft = {"templates": payload["templates"]} if "templates" in payload else {}
                return control.preview(target.name, payload.get("friend"), **draft)
            with self._start_lock:
                if operation == "update" and payload == {
                    "account": payload.get("account"),
                    "enabled": False,
                }:
                    target = self.account(settings, payload.get("account"))
                    return control.update(target.name, {"enabled": False})
                self._ensure_idle(settings, jobs)
                if operation == "add":
                    if payload.get("enabled") is True:
                        raise PanelError("新增账号默认停用，扫码登录成功后会自动启用")
                    return control.add(payload)
                target = self.account(settings, payload.get("account"))
                if operation == "delete":
                    with exclusive_run(DataDir(settings.data_dir).lock_file):
                        return control.delete(target.name)
                if operation == "resume":
                    # 开关只决定是否参与自动发送；每次真实发送前都会现场核验登录与环境。
                    return control.resume(target.name)
                if operation == "update":
                    fields = {k: v for k, v in payload.items() if k != "account"}
                    if isinstance(fields.get("proxy"), str) and fields["proxy"].strip():
                        # 直接粘贴的代理地址在此落盘 .env，config.toml 只保存内部引用。
                        resolved = self._resolve_new_account_proxy(fields["proxy"])
                        fields = {**fields, "proxy": resolved}
                    return control.update(target.name, fields)
                raise PanelError("未知账号操作")
        except (ValueError, ConfigEditError, RunBusyError) as exc:
            raise PanelError(str(exc)) from exc

    def scheduler_status(self) -> dict[str, Any]:
        if self.scheduler is not None:
            return self.scheduler.status()
        return {
            "enabled": self.settings().schedule_enabled,
            "running": False,
            "leader": False,
            "error": None,
        }

    def control_schedule(self, enabled: bool) -> dict[str, Any]:
        import tomlkit

        from dyspark.config_edit import ConfigEditError, edit_config

        def update(doc):
            if "schedule" not in doc:
                doc["schedule"] = tomlkit.table()
            doc["schedule"]["enabled"] = enabled

        try:
            edit_config(self.config_path, update)
        except ConfigEditError as exc:
            raise PanelError(str(exc)) from exc
        return {
            "enabled": enabled,
            "message": "自动计划已启用，服务运行时按配置触发"
            if enabled
            else "自动计划已停用；已开始的任务仍按账号保护执行",
        }

    def control_mcp(self, enabled: bool) -> dict[str, Any]:
        import tomlkit

        from dyspark.config_edit import ConfigEditError, edit_config

        def update(doc):
            if "mcp" not in doc:
                doc["mcp"] = tomlkit.table()
            doc["mcp"]["enabled"] = enabled

        try:
            edit_config(self.config_path, update)
        except ConfigEditError as exc:
            raise PanelError(str(exc)) from exc
        return {
            "enabled": enabled,
            "message": "MCP 已开启。" if enabled else "MCP 已关闭。",
        }

    def notifications(self, test: bool = False) -> dict[str, Any]:
        from dyspark.notify import Notifier

        settings = self.settings()
        key = settings.notify.serverchan_key
        token = settings.notify.pushplus_token
        result = {
            "enabled": settings.notify.enabled,
            "push_configured": bool(key),
            "push_key_tail": key[-4:] if key else None,
            "serverchan_key": key or "",
            "pushplus_configured": bool(token),
            "pushplus_token_tail": token[-4:] if token else None,
            "pushplus_token": token or "",
        }
        state = read_json(settings.data_dir / "notify-state.json", None)
        last = state.get("last") if isinstance(state, dict) else None
        if isinstance(last, dict) and last.get("ok") is False and isinstance(last.get("at"), str):
            result["last_failure"] = {
                "at": last["at"],
                "title": last.get("title") if isinstance(last.get("title"), str) else "",
                "detail": last.get("detail") if isinstance(last.get("detail"), str) else "",
            }
        if test:
            if not result["enabled"]:
                raise PanelError("推送通知开关当前是关闭的，请先打开「推送通知」右上角的开关")
            if not result["push_configured"] and not result["pushplus_configured"]:
                raise PanelError(
                    "请先保存 Server酱 SendKey 或 PushPlus token（可展开下方步骤获取），"
                    "保存后即可重试，无需重启"
                )
            notifier = Notifier(settings.notify, state_path=settings.data_dir / "notify-state.json")
            if not notifier.push(
                "通知测试",
                "面板通知已连接；这是一条手动测试消息。",
                dedupe=False,
            ):
                raise PanelError("推送未被全部渠道确认，请检查各渠道配置与网络（失败原因见状态行）")
            result["message"] = "测试通知已发送"
        return result

    def save_notifications(
        self,
        serverchan_key: str | None,
        pushplus_token: str | None,
        enabled: bool | None,
        clear_serverchan: bool = False,
        clear_pushplus: bool = False,
    ) -> dict[str, Any]:
        """Store notification secrets in the sibling .env; values are echoed back for display."""
        from dyspark.locking import LockBusyError, file_lock

        try:
            with file_lock(self.config_path.parent / ".env.edit.lock", timeout=5):
                return self._save_notifications(
                    serverchan_key,
                    pushplus_token,
                    enabled,
                    clear_serverchan,
                    clear_pushplus,
                )
        except LockBusyError as exc:
            raise PanelError("通知配置正在更新，请稍后重试") from exc

    def _save_notifications(
        self,
        serverchan_key: str | None,
        pushplus_token: str | None,
        enabled: bool | None,
        clear_serverchan: bool,
        clear_pushplus: bool,
    ) -> dict[str, Any]:
        updates: dict[str, str] = {}
        cleared: list[str] = []
        for name, raw, label, clear in (
            ("SPARK_SERVERCHAN_KEY", serverchan_key, "Server酱 SendKey", clear_serverchan),
            ("SPARK_PUSHPLUS_TOKEN", pushplus_token, "PushPlus token", clear_pushplus),
        ):
            if clear:
                updates[name] = ""  # 空值即移除该渠道
                cleared.append(label)
                continue
            if raw is not None and not isinstance(raw, str):
                raise PanelError(f"{label} 必须是文字或 null")
            if raw:
                if any(unicodedata.category(ch) == "Cc" for ch in raw):
                    raise PanelError(f"{label} 不能包含换行或控制字符")
                value = raw.strip()
                if not value:
                    raise PanelError(f"{label} 不能只填空白")
                updates[name] = value
        for name, value in updates.items():
            if name in os.environ and os.environ[name].strip() != value:
                raise PanelError(
                    f"{name} 由进程环境变量管理，提交的凭据无法生效。"
                    "请通过部署配置修改；如需面板管理，移除该变量并重新启动应用"
                    "（Docker 部署需重新创建容器）。"
                )
        flags = {"enabled": enabled} if enabled is not None else {}
        env_path = self.config_path.parent / ".env"
        previous = env_path.read_bytes() if env_path.exists() else None
        try:
            if updates:
                self._write_env(env_path, updates)
            if flags:
                self._set_notify_flags(flags)
            result = self.notifications()
        except BaseException:
            if updates:
                from dyspark.store import write_private_bytes

                if previous is None:
                    env_path.unlink(missing_ok=True)
                else:
                    write_private_bytes(env_path, previous)
            raise
        overridden = [name for name in updates if name in os.environ]
        message = "通知设置已保存" if overridden else "通知设置已保存，立即生效"
        if cleared:
            message += f"；已移除 {'、'.join(cleared)} 的配置"
        if overridden:
            message += f"；注意：系统环境变量已有 {'、'.join(overridden)}，会覆盖 .env 中的值"
        result["message"] = message
        return result

    def _write_env(self, path: Path, updates: dict[str, str]) -> None:
        """Upsert dotenv lines in place, preserving every other line and comment."""
        from dyspark.store import write_private_bytes

        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8-sig").splitlines()
            except UnicodeDecodeError as exc:
                raise PanelError(f"{path.name} 不是 UTF-8 编码，请先手工修正后再保存") from exc
        else:
            lines = []
        for name, value in updates.items():
            marker = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
            quoted = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
            found = False
            for index, line in enumerate(lines):
                if marker.match(line):
                    lines[index] = f"{name}={quoted}"
                    found = True
            if not found:
                lines.append(f"{name}={quoted}")
        write_private_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))

    def _set_notify_flags(self, flags: dict[str, bool]) -> None:
        import tomlkit

        from dyspark.config_edit import ConfigEditError, edit_config

        def update(doc):
            if "notify" not in doc:
                doc["notify"] = tomlkit.table()
            for name, value in flags.items():
                doc["notify"][name] = value

        try:
            edit_config(self.config_path, update)
        except ConfigEditError as exc:
            raise PanelError(str(exc)) from exc

    def logs(
        self, run: str | None = None, lines: int = 80, include_screenshots: bool = True
    ) -> dict[str, Any]:
        settings = self.settings()
        try:
            result = run_logs(DataDir(settings.data_dir), run, lines)
        except ValueError as exc:
            raise PanelError(str(exc)) from exc
        if not include_screenshots:
            result.pop("screenshots", None)
        return result

    def service_log(self, name: str, lines: int = 80) -> dict[str, Any]:
        """Tail of a resident service's log (``gui`` or ``mcp``), for panel and MCP."""
        settings = self.settings()
        try:
            return service_logs(DataDir(settings.data_dir), name, lines)
        except ValueError as exc:
            raise PanelError(str(exc)) from exc

    def schedule(self) -> dict[str, Any]:
        """Configured times plus trigger hints for Windows / Linux / Docker."""
        from dyspark.schedule import describe

        return {**describe(self.settings(), self.config_path), "scheduler": self.scheduler_status()}

    def save_schedule(self, times: object) -> dict[str, Any]:
        from dyspark.config_edit import ConfigEditError
        from dyspark.schedule import set_times

        try:
            set_times(self.config_path, times)
        except ConfigEditError as exc:
            raise PanelError(str(exc)) from exc
        return {
            **self.schedule(),
            "message": "每日时间已保存。",
        }

    def fetch_conversations(
        self,
        account: str | None = None,
        sparks_only: bool = False,
        wait_seconds: float = 20,
    ) -> dict[str, Any]:
        """Start a friends-list job; return like ``run`` (poll with run_status if still running)."""
        settings = self.settings()
        account = _optional_account(account)
        wait = _clamp(wait_seconds, 20)
        args = ["friends", "--json"]
        if sparks_only:
            args.append("--sparks-only")
        if account:
            args.extend(["--account", self.account(settings, account).name])
        job = self._start(settings, "friends", args)
        return self._job_view(settings, job, wait)

    def list_friends(self, account: str | None = None) -> dict[str, Any]:
        settings = self.settings()
        account = _optional_account(account)
        accounts = [self.account(settings, account)] if account else settings.accounts
        return {"accounts": [{"name": a.name, "friends": list(a.friends)} for a in accounts]}

    # -------------------------------------------------------------- edits
    def add_friend(self, account: str | None, name: str) -> dict[str, Any]:
        return self._edit(add_friend, account, name, "已添加")

    def remove_friend(self, account: str | None, name: str) -> dict[str, Any]:
        return self._edit(remove_friend, account, name, "已移除")

    def replace_friends(self, account: str | None, names: list[str]) -> dict[str, Any]:
        target = self.account(self.settings(), account)
        try:
            friends = set_friends(self.config_path, target.name, names)
        except FriendEditError as exc:
            raise PanelError(str(exc)) from exc
        return {
            "account": target.name,
            "friends": friends,
            "message": f"已写入 {len(friends)} 个好友，下次运行生效",
        }

    def _edit(
        self,
        edit: Callable[[Path, str, object], list[str]],
        account: str | None,
        name: str,
        done: str,
    ) -> dict[str, Any]:
        target = self.account(self.settings(), account)
        try:
            friends = edit(self.config_path, target.name, name)
        except FriendEditError as exc:
            raise PanelError(str(exc)) from exc
        return {
            "account": target.name,
            "friends": friends,
            "message": f"{done}「{name.strip()}」，下次运行生效",
        }

    # --------------------------------------------------------------- jobs
    def run(
        self,
        account: str | None = None,
        dry_run: bool = False,
        wait_seconds: float = 20,
        *,
        trigger: str = "mcp",
    ) -> dict[str, Any]:
        settings = self.settings()
        account = _optional_account(account)
        wait = _clamp(wait_seconds, 20)
        args = ["run", "--now", "--trigger", trigger]
        if account:
            target = self.account(settings, account)
            if not target.enabled:
                raise PanelError(f"账号 {target.name} 已停用，请在账号管理中启用后再发送")
            args += ["--account", target.name]
        elif not any(target.enabled for target in settings.accounts):
            raise PanelError("没有启用的账号，请在账号管理中启用后再发送")
        if dry_run:
            args.append("--dry-run")
        job = self._start(settings, "run", args)
        return self._job_view(settings, job, wait)

    def check(self, account: str | None = None, wait_seconds: float = 50) -> dict[str, Any]:
        settings = self.settings()
        account = _optional_account(account)
        wait = _clamp(wait_seconds, 50)
        args = ["check"]
        if account:
            args += ["--account", self.account(settings, account).name]
        job = self._start(settings, "check", args)
        return self._job_view(settings, job, wait)

    def run_status(self, job_id: str | None = None, wait_seconds: float = 0) -> dict[str, Any]:
        settings = self.settings()
        wait = _clamp(wait_seconds, 0)
        jobs = self.jobs(settings)
        if job_id is None:
            recent = [j for j in jobs.recent(None) if j["kind"] in POLL_JOB_KINDS]
            if not recent:
                return {
                    "job": None,
                    "message": "还没有通过面板启动过发送、检查或会话拉取任务",
                }
            job_id = recent[0]["job_id"]
        return self._job_view(settings, self._job(settings, job_id), wait)

    def _job_view(self, settings: Settings, job: Job, wait: float) -> dict[str, Any]:
        info = self.jobs(settings).wait(job, wait)
        if info["state"] == "running":
            info["message"] = f"任务仍在运行，请稍后查询（job_id={job.id}）"
        elif info["exit_code"] == 75 or (info.get("result") or {}).get("busy"):
            info["message"] = "另一个 dyspark 任务正在运行，本次没有执行"
        elif (
            info["state"] == "finished"
            and info["exit_code"] == 0
            and info.get("kind") == "run"
            and (info.get("result") or {}).get("accounts") == []
        ):
            info["message"] = (
                "本次未处理任何账号，未发送消息。请检查账号是否暂停、目标列表是否为空或今日已处理。"
            )
        return info

    # -------------------------------------------------------------- login
    def account_login_start(
        self, wait_seconds: float | None = None, proxy_ref: str | None = None
    ) -> dict[str, Any]:
        """Log into a fresh profile; the child registers it after success only.

        ``wait_seconds`` 为 None 时沿用原行为（阻塞到二维码就绪）；面板传 0，
        让弹窗先显示启动进度，二维码交给登录轮询接上。``proxy_ref`` 是可选的
        ``${VAR}`` 代理引用：登录浏览器与登记后的账号都走这条路线，仅作用于本账号。
        """
        settings = self.settings()
        while True:
            name = "account_" + secrets.token_hex(6)
            folder = settings.data_dir / "accounts" / name
            if (
                not folder.exists()
                and not folder.is_symlink()
                and not any(a.name.casefold() == name.casefold() for a in settings.accounts)
            ):
                break
        args = [
            "login",
            name,
            "--mode",
            "web",
            "--timeout",
            f"{settings.login.timeout_seconds:g}",
            "--new-account",
        ]
        if self.internal_login:
            args.append("--internal-relay")
        if proxy_ref:
            args += ["--proxy-ref", self._resolve_new_account_proxy(proxy_ref)]
        job = self._start(settings, "login", args)
        return self.login_payload(settings, job, _clamp(wait_seconds, LOGIN_START_WAIT), first=True)

    def _proxy_value(self, proxy_ref: str) -> str:
        """Validate a proxy URL or variable name/reference, returning the direct URL."""
        trimmed = proxy_ref.strip()
        try:
            if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", trimmed):
                if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in trimmed):
                    raise PanelError("代理地址不能包含空格、换行或控制字符")
                validate_proxy(trimmed, "代理地址")
                return trimmed
            ref = validate_proxy_reference(trimmed)
            if not ref:
                raise PanelError("代理为空：留空表示直接连接")
            expanded = expand_env(ref, load_env(self.config_path), "代理").strip()
            if not expanded:
                raise PanelError(f"代理 {ref} 未在 .env 中定义或内容为空")
            validate_proxy(expanded, "代理")
            return expanded
        except SettingsError as exc:
            raise PanelError(str(exc)) from exc

    def _resolve_new_account_proxy(self, proxy_ref: str) -> str:
        """Resolve an add/update proxy into a ``${VAR}`` reference.

        Direct proxy URLs are persisted into .env under a stable generated name
        (credentials stay out of config.toml); names/references pass through.
        """
        import hashlib

        from dyspark.locking import LockBusyError, file_lock

        trimmed = proxy_ref.strip()
        url = self._proxy_value(trimmed)
        if not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", trimmed):
            return validate_proxy_reference(trimmed)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8].upper()
        name = "SPARK_PROXY_" + digest
        if load_env(self.config_path).get(name) != url:
            try:
                with file_lock(self.config_path.parent / ".env.edit.lock", timeout=5):
                    self._write_env(self.config_path.parent / ".env", {name: url})
            except LockBusyError as exc:
                raise PanelError(".env 正在被其他操作更新，请稍后重试") from exc
        return "${" + name + "}"

    def check_proxy(self, proxy_ref: str) -> dict[str, Any]:
        """Dial the configured IP echo services through the given proxy; no state changes."""
        import time

        import httpx

        from dyspark.egress import parse_ip
        from dyspark.store import mask_proxy

        if not isinstance(proxy_ref, str) or not proxy_ref.strip():
            raise PanelError("请先填写代理地址")
        url = self._proxy_value(proxy_ref)
        settings = self.settings()
        if url.lower().startswith("socks4://"):
            import tempfile
            from dataclasses import replace

            from dyspark.browser import open_context
            from dyspark.egress import lookup

            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="dyspark-proxy-check-") as directory:
                    isolated = replace(settings, data_dir=Path(directory))
                    account = Account(name="proxy_probe", friends=(), proxy=url)
                    with open_context(isolated, account, headless=True) as context:
                        ip = lookup(context, settings.ip_echo_urls[:3], timeout_ms=10_000)
            except Exception as exc:
                reason = str(exc).replace(url, mask_proxy(url))[:120]
                raise PanelError(f"连接失败：{reason}") from exc
            return {
                "egress_ip": ip,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "message": f"连通正常，出口 IP {ip}",
            }
        failures: list[str] = []
        for echo in settings.ip_echo_urls[:3]:
            started = time.monotonic()
            try:
                with httpx.Client(
                    proxy=url, timeout=httpx.Timeout(10.0, connect=5.0), trust_env=False
                ) as client:
                    response = client.get(echo)
                ip = parse_ip(response.text)
            except httpx.ProxyError:
                failures.append("代理握手失败（地址或凭据有误）")
                continue
            except httpx.ConnectError:
                failures.append("无法连接代理服务器")
                continue
            except httpx.TimeoutException:
                failures.append("连接超时")
                continue
            except Exception as exc:  # surface a short reason; never echo the raw proxy URL
                failures.append(str(exc).replace(url, mask_proxy(url))[:120])
                continue
            if not ip:
                failures.append(f"{echo} 未返回可识别的出口 IP")
                continue
            return {
                "egress_ip": ip,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "message": f"连通正常，出口 IP {ip}",
            }
        raise PanelError("连接失败：" + "；".join(dict.fromkeys(failures)))

    def login_start(
        self,
        account: str | None = None,
        timeout_minutes: float | None = None,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        settings = self.settings()
        target = self.account(settings, account)
        minutes = settings.login.timeout_minutes if timeout_minutes is None else timeout_minutes
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int | float)
            or not 1 <= minutes <= 60
        ):
            raise PanelError("timeout_minutes 必须在 1 到 60 之间")
        jobs = self.jobs(settings)
        for active in jobs.active():
            if active["kind"] == "login" and target.name in (active.get("args") or []):
                return self.login_payload(settings, jobs.get(active["job_id"]), 0, first=False)
        args = ["login", target.name, "--mode", "web", "--timeout", f"{minutes * 60:g}"]
        if self.internal_login:
            args.append("--internal-relay")
        job = self._start(settings, "login", args)
        return self.login_payload(settings, job, _clamp(wait_seconds, LOGIN_START_WAIT), first=True)

    def login_status(self, session_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        settings = self.settings()
        job = self._job(settings, session_id, "login")
        return self.login_payload(settings, job, _clamp(wait_seconds, 0), first=False)

    def login_cancel(self, session_id: str) -> dict[str, Any]:
        settings = self.settings()
        job = self._job(settings, session_id, "login")
        jobs = self.jobs(settings)
        try:
            info = jobs.describe(job)
            if login_status_of(info) in {"finalizing", "success"}:
                return self.login_payload(settings, job, 0, first=False)
            if info["state"] in {"running", "blocked"}:
                if (info.get("session") or {}).get("url"):
                    try:
                        self._login_relay(settings, info, "POST", "cancel", {})
                    except PanelError:
                        if login_status_of(jobs.describe(job)) in {"finalizing", "success"}:
                            return self.login_payload(settings, job, 0, first=False)
                jobs.cancel(job)
        except JobError as exc:
            raise PanelError(str(exc)) from exc
        return self.login_payload(settings, job, 0, first=False)

    def login_refresh_qr(self, session_id: str) -> dict[str, Any]:
        settings = self.settings()
        job = self._job(settings, session_id, "login")
        info = self.jobs(settings).describe(job)
        result = self._login_relay(settings, info, "POST", "qr/refresh", {})
        return {"message": result.get("message") or "二维码刷新请求已接收"}

    def login_sms(self, session_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in ("send", "verify"):
            raise PanelError("不支持的短信操作")
        settings = self.settings()
        job = self._job(settings, session_id, "login")
        info = self.jobs(settings).describe(job)
        result = self._login_relay(settings, info, "POST", f"sms/{action}", payload)
        return {"message": result.get("message") or "请求已提交"}

    @staticmethod
    def _login_relay(
        settings: Settings,
        info: dict[str, Any],
        method: str,
        tail: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = request_login(settings.login, info, method, tail, payload)
            result = json.loads(response.content)
            if not isinstance(result, dict):
                raise PanelError("登录验证页面响应无效")
            if response.status not in (200, 202):
                raise PanelError(result.get("message") or "登录验证页面拒绝请求")
            return result
        except (LoginTransportError, ValueError) as exc:
            if isinstance(exc, PanelError):
                raise
            if isinstance(exc, LoginTransportError):
                raise PanelError(str(exc)) from None
            raise PanelError("无法连接登录验证页面，请检查会话是否已结束") from None

    def login_content(
        self, session_id: str, method: str, tail: str, payload: dict[str, Any] | None = None
    ) -> LoginResponse:
        settings = self.settings()
        job = self._job(settings, session_id, "login")
        info = self.jobs(settings).describe(job)
        try:
            return request_login(settings.login, info, method, tail, payload)
        except LoginTransportError as exc:
            raise PanelError(str(exc)) from None

    def login_payload(
        self, settings: Settings, job: Job, wait: float, *, first: bool
    ) -> dict[str, Any]:
        jobs = self.jobs(settings)
        before = login_status_of(jobs.describe(job))

        def changed(info: dict[str, Any]) -> bool:
            status = login_status_of(info)
            if first:
                return status not in ("starting",) and (
                    status != "pending" or bool(info.get("session", {}).get("qr_path"))
                )
            return status != before

        info = jobs.wait(job, wait, until=changed)
        session = info.get("session") or {}
        status = login_status_of(info)
        started_at = info.get("started_at") or session.get("started_at")
        elapsed = session.get("elapsed_seconds", 0)
        if status not in FINISHED and started_at:
            try:
                started = datetime.fromisoformat(started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=settings.zone)
                elapsed = max(0, int((datetime.now(settings.zone) - started).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                elapsed = 0
        payload = {
            "session_id": job.id,
            "account": session.get("account") or _arg_account(info),
            "status": status,
            "finished": status in FINISHED,
            "stage": session.get("stage") or "browser",
            "login_source": session.get("login_source"),
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "qr_version": session.get("qr_version", 0),
            "screen_version": session.get("screen_version", 0),
            "route_known": "proxy" in session or "proxy" in (info.get("result") or {}),
            "message": (
                info.get("message")
                if info["state"] == "blocked"
                else session.get("message") or info.get("message")
            ),
            "url": session.get("url"),
            "expires_at": session.get("expires_at"),
            "qr_path": session.get("qr_path"),
            "screen_path": session.get("screen_path"),
            "novnc_url": session.get("novnc_url"),
            "egress_ip": session.get("egress_ip") or (info.get("result") or {}).get("egress_ip"),
            "proxy": session.get("proxy") or (info.get("result") or {}).get("proxy"),
            "job_state": info["state"],
            "status_text": STATUS_TEXT.get(status, ""),
            "phone_input": session.get("phone_input", True),
            "secondary_verification": session.get("secondary_verification", False),
            "phone_hint": session.get("phone_hint"),
            "sms_sent": session.get("sms_sent", False),
            "sms_action": session.get("sms_action"),
            "qr_refreshing": session.get("qr_refreshing", False),
            "new_account": "--new-account" in (info.get("args") or []),
            "proxy_ref": _arg_value(info, "--proxy-ref"),
        }
        if payload["new_account"] and status == "failed":
            result = info.get("result") or {}
            payload["message"] = result.get("error") or payload["message"]
            if result.get("duplicate_of"):
                # 去重拒绝：把已有账号交给前端，弹窗可以给出「前往」入口。
                payload["duplicate_of"] = result["duplicate_of"]
                payload["duplicate_label"] = result.get("duplicate_label")
        elif payload["new_account"] and status == "finalizing":
            payload["message"] = "已通过抖音验证，正在核对运行环境并保存新账号…"
        if (
            payload["new_account"]
            and payload["account"]
            and status in FINISHED
            and status != "success"
            and info["state"] != "running"  # 进程还活着时文件被占用，删不干净
        ):
            marker = read_json(job.file("pending-account.json"), {})
            owner = marker.get("owner") if isinstance(marker, dict) else None
            if owner and marker.get("account") == payload["account"]:
                discard_unregistered_profile(
                    self.config_path, settings.data_dir, payload["account"], owner=owner
                )
        if status not in FINISHED and session.get("url"):
            try:
                live = self._login_relay(settings, info, "GET", "state")
                for key in (
                    "phone_input",
                    "secondary_verification",
                    "phone_hint",
                    "sms_sent",
                    "sms_action",
                    "sms",
                    "qr_refreshing",
                ):
                    if key in live:
                        payload[key] = live[key]
            except PanelError:
                pass  # A starting/stopping relay must not break job status polling.
        if info.get("output_tail") and status in ("failed", "starting"):
            payload["output_tail"] = info["output_tail"]
        return payload

    def media_file(self, path: str) -> Path:
        """Resolve a PNG under data/runs or data/jobs (traversal-safe)."""
        settings = self.settings()
        root = Path(settings.data_dir).resolve()
        try:
            target = Path(path).expanduser().resolve()
        except OSError as exc:
            raise PanelError(f"无法读取图片：{exc}") from exc
        allowed = (root / "runs", root / "jobs")
        if not any(base == target or base in target.parents for base in allowed):
            raise PanelError("只能读取运行或登录会话目录中的图片")
        if target.suffix.lower() != ".png" or not target.is_file():
            raise PanelError("图片不存在或不是 PNG")
        return target


def login_status_of(info: dict[str, Any]) -> str:
    session = info.get("session") or {}
    status = session.get("status")
    if "--new-account" in (info.get("args") or []) and status == "success":
        if info["state"] == "running":
            return "finalizing"
        return "success" if info.get("exit_code") == 0 else "failed"
    if info["state"] == "running":
        return status or "starting"
    if status and session.get("finished"):
        return status
    # 退出码兜底只服务「会话文件没写完就退出」的老会话；被强杀在启动期的会话
    # 已写出 starting 状态，进程又可能带着 0 退出码退场，绝不能当成登录成功。
    if info.get("exit_code") == 0 and not status:
        return "success"
    return "failed"


def _arg_account(info: dict[str, Any]) -> str | None:
    args = info.get("args") or []
    return args[1] if len(args) > 1 else None


def _arg_value(info: dict[str, Any], flag: str) -> str | None:
    args = info.get("args") or []
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None
