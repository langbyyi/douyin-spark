"""Account commands: login, environment checks and conversation discovery."""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from dyspark.account_health import HealthStore
from dyspark.config import (
    Account,
    Settings,
    SettingsError,
    expand_env,
    load_env,
    read_settings,
    validate_account_name,
    validate_proxy,
)
from dyspark.jobs import JobReporter
from dyspark.logs import brief_exc
from dyspark.notify import Notifier
from dyspark.store import (
    NEED_LOGIN_NOTE,
    PROXY_CHANGED_NOTE,
    DataDir,
    LoginRecord,
    SparkRecord,
    exclusive_run,
    mask_proxy,
    proxy_fingerprint,
    write_json_atomic,
)

log = logging.getLogger("dyspark")


def _check_failure_code(status: str) -> int:
    return 3 if status == "blocked" else 2 if status in {"logged_out", "ip_mismatch"} else 1


def cmd_account_control(args: argparse.Namespace) -> int:
    from dyspark.panel import Backend, PanelError

    backend = Backend(args.config.expanduser().resolve(), args.data)
    operation = "enable" if args.command in {"enable", "resume"} else "disable"
    payload: dict[str, Any] = {"account": args.account}
    try:
        result = backend.account_control(operation, payload)
    except PanelError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result["message"])
    return 0


def cmd_login(settings: Settings, args: argparse.Namespace, reporter: JobReporter) -> int:
    from dyspark.accounts import (
        add_account,
        discard_unregistered_profile,
        find_duplicate_account,
        validate_proxy_reference,
    )
    from dyspark.browser import BrowserLaunchError
    from dyspark.config_edit import ConfigEditError
    from dyspark.login import LoginError, LoginMode, login_account

    mode = LoginMode(args.mode or ("qr" if settings.cloud else "window"))
    if getattr(args, "internal_relay", False):
        settings = replace(
            settings, login=replace(settings.login, bind="127.0.0.1", port=0, public_url=None)
        )
    new_account = getattr(args, "new_account", False)
    registration_owner = None
    reporter.data = {"account": args.account, "mode": mode.value}
    try:
        if mode is LoginMode.QR and settings.cloud:
            if not (settings.notify.serverchan_key or settings.login.public_url):
                raise LoginError(
                    "qr 模式需要 SPARK_SERVERCHAN_KEY 或 login.public_url 之一，"
                    "否则二维码无法送达手机；请先配置，或改用 --mode web"
                )
            if not (settings.notify.serverchan_key and settings.login.public_url):
                log.warning(
                    "qr 模式建议同时设置 SPARK_SERVERCHAN_KEY 和 login.public_url："
                    "缺一则二维码只能以文件路径或控制台链接之一呈现"
                )
        with exclusive_run(DataDir(settings.data_dir).lock_file):
            proxy_ref = ""
            if new_account:
                name = validate_account_name(args.account)
                current = read_settings(args.config.expanduser().resolve(), settings.data_dir)
                folder = settings.data_dir / "accounts" / name
                if (
                    any(a.name.casefold() == name.casefold() for a in current.accounts)
                    or folder.exists()
                    or folder.is_symlink()
                ):
                    raise LoginError("账号标识已有配置或保留资料，请重新开始添加账号")
                proxy_ref = validate_proxy_reference(getattr(args, "proxy_ref", None) or "")
                expanded_proxy = ""
                if proxy_ref:
                    expanded_proxy = expand_env(
                        proxy_ref, load_env(args.config.expanduser().resolve()), "添加账号的代理"
                    ).strip()
                    if not expanded_proxy:
                        raise LoginError(f"代理引用 {proxy_ref} 展开后为空，请在 .env 中定义该变量")
                    validate_proxy(expanded_proxy, "添加账号的代理")
                account = Account(
                    name=name, friends=(), enabled=False, proxy=expanded_proxy or None
                )
                registration_owner = secrets.token_hex(16)
                if args.job_dir:
                    write_json_atomic(
                        args.job_dir / "pending-account.json",
                        {"account": name, "owner": registration_owner},
                    )
                folder.mkdir(parents=True, exist_ok=False)
                write_json_atomic(
                    folder / ".pending-registration.json", {"owner": registration_owner}
                )
            else:
                account = settings.account(args.account)
            masked = mask_proxy(account.proxy)
            reporter.data["proxy"] = masked or None
            print(f"[login] 账号 {account.name} 代理：{masked or '未配置，将直连'}")
            notifier = Notifier(settings.notify, state_path=settings.data_dir / "notify-state.json")
            record = login_account(
                settings,
                account,
                mode,
                notifier,
                args.timeout,
                session_dir=args.job_dir,
                notify_success=not new_account,
            )
            config_path = args.config.expanduser().resolve()
            if new_account:
                duplicate = find_duplicate_account(config_path, settings.data_dir, record.user_id)
                if duplicate is not None:
                    existing, label = duplicate
                    reporter.data["duplicate_of"] = existing.name
                    reporter.data["duplicate_label"] = label
                    raise LoginError(
                        f"此抖音账号已添加为「{label}」；登录失效请在原账号上重新登录，"
                        "对象、消息模板与备注都会保留"
                    )
                registered: dict[str, Any] = {"name": account.name, "enabled": True}
                if proxy_ref:
                    registered["proxy"] = proxy_ref
                add_account(config_path, registered)
                (folder / ".pending-registration.json").unlink(missing_ok=True)
                reporter.data["account_created"] = True
                notifier.push(
                    "登录成功",
                    f"账号 {account.name} 已登录，出口IP {record.egress_ip}",
                    dedupe=False,
                )
            # 重新登录不碰开关：enabled 是用户意愿，登录是否有效由健康记录表达。
            # 手动停用的账号重新扫码后保持停用，不会悄悄恢复参与自动发送。
    except (LoginError, ConfigEditError, SettingsError) as exc:
        log.error("登录失败：%s", exc)
        reporter.data["error"] = str(exc)
        return 2
    except BrowserLaunchError as exc:
        log.error("%s", exc)
        reporter.data["error"] = str(exc)
        return 1
    finally:
        # 本次登录没登记成账号（失败/超时/被取消）时，收回它留下的浏览器资料。
        if (
            new_account
            and registration_owner
            and discard_unregistered_profile(
                args.config.expanduser().resolve(),
                settings.data_dir,
                args.account,
                owner=registration_owner,
            )
        ):
            log.info("已清理未完成的登录资料：%s", args.account)
    reporter.data.update(logged_in=True, egress_ip=record.egress_ip, proxy=record.proxy or None)
    print(f"已登录：{record.account} 出口IP={record.egress_ip} 代理={record.proxy or '-'}")
    return 0


def cmd_check(
    settings: Settings, args: argparse.Namespace, run_dir: Path, reporter: JobReporter
) -> int:
    from dyspark.account_profile import OwnProfile
    from dyspark.live_check import check_live_environment
    from dyspark.session import live_session

    data = DataDir(settings.data_dir)
    health = HealthStore(settings.data_dir)
    names = args.accounts or [a.name for a in settings.accounts]
    results: list[dict[str, Any]] = []
    reporter.data = {"run": run_dir.name, "accounts": results}
    worst = 0
    with exclusive_run(data.lock_file):
        for name in names:
            account = settings.account(name)
            record = LoginRecord.load(data.login_file(name))
            item: dict[str, Any] = {
                "account": name,
                "login_record": record is not None,
                "egress_ip_at_login": record.egress_ip if record else None,
            }
            results.append(item)
            print(f"== {name}: 登录记录{'存在' if record else '缺失'}")
            fields = {
                "login_ip": record.egress_ip if record else None,
                "proxy_fingerprint": proxy_fingerprint(account.proxy),
            }
            if record is None or record.proxy_fingerprint != fields["proxy_fingerprint"]:
                status = "logged_out" if record is None else "ip_mismatch"
                reason = NEED_LOGIN_NOTE if record is None else PROXY_CHANGED_NOTE
                item.update(ok=False, health=health.record(name, status, reason, **fields))
                worst = max(worst, 2)
                reporter.progress(reporter.data)
                continue
            try:
                with live_session(settings, run_dir, account) as session:
                    result = check_live_environment(
                        session, record, account, settings, health=health
                    )
                    item["ok"] = result.ok
                    if result.current_ip is not None:
                        item["egress_ip_now"] = result.current_ip
                        item["ip_consistent"] = result.status != "ip_mismatch"
                        at_login = record.egress_ip if record else "-"
                        consistent = "一致" if item["ip_consistent"] else "不一致"
                        print(
                            f"   出口IP 当前 {result.current_ip}；登录时 {at_login}；{consistent}"
                        )
                    if result.state is not None:
                        item["page_state"] = result.state.value
                        print(f"   聊天页状态：{result.state.value}")
                    item["health"] = health.view(name)
                    if result.ok and isinstance(result.profile, OwnProfile):
                        item["nickname"] = record.nickname
                        item["avatar_url"] = record.avatar_url
                    try:
                        session.snapshot("check")
                    except Exception:
                        log.debug("check snapshot failed", exc_info=True)
            except Exception as exc:
                log.debug("check failed", exc_info=True)
                item["error"] = brief_exc(exc)
                item.update(
                    ok=False, health=health.record(name, "network_error", item["error"], **fields)
                )
                print(f"   检查失败：{item['error']}")
                worst = max(worst, 1)
                reporter.progress(reporter.data)
                continue
            if not item["ok"]:
                worst = max(worst, _check_failure_code(result.status))
            reporter.progress(reporter.data)
    return worst


def cmd_friends(
    settings: Settings,
    args: argparse.Namespace,
    run_dir: Path,
    reporter: JobReporter,
    *,
    config_path: Path,
) -> int:
    """Open each account's chat page and dump the conversation list."""
    import json

    from dyspark.conversation_cache import conversation_metadata, save_conversations
    from dyspark.friends import FriendEditError, set_friends
    from dyspark.live_check import check_live_environment
    from dyspark.session import live_session

    def say(message: str) -> None:
        print(message, file=sys.stderr if args.as_json else sys.stdout)

    data = DataDir(settings.data_dir)
    health = HealthStore(settings.data_dir)
    names = args.accounts or [a.name for a in settings.accounts]
    payload: list[dict[str, Any]] = []
    reporter.data = {"run": run_dir.name, "accounts": payload}
    worst = 0
    with exclusive_run(data.lock_file):
        for name in names:
            account = settings.account(name)
            item: dict[str, Any] = {"account": name, "conversations": []}
            payload.append(item)
            say(f"== {name}")
            record = LoginRecord.load(data.login_file(name))
            fields = {
                "login_ip": record.egress_ip if record else None,
                "proxy_fingerprint": proxy_fingerprint(account.proxy),
            }
            if record is None or record.proxy_fingerprint != fields["proxy_fingerprint"]:
                status = "logged_out" if record is None else "ip_mismatch"
                reason = NEED_LOGIN_NOTE if record is None else PROXY_CHANGED_NOTE
                item.update(ok=False, health=health.record(name, status, reason, **fields))
                say(f"   核验失败：{reason}")
                worst = max(worst, _check_failure_code(status))
                reporter.progress(reporter.data)
                continue
            try:
                with live_session(settings, run_dir, account) as session:
                    result = check_live_environment(
                        session, record, account, settings, health=health
                    )
                    item.update(ok=result.ok, health=health.view(name))
                    if result.state is not None:
                        item["page_state"] = result.state.value
                    if not result.ok:
                        say(f"   核验失败：{result.reason}")
                        worst = max(worst, _check_failure_code(result.status))
                        reporter.progress(reporter.data)
                        continue
                    rows = session.list_conversations()
                    all_conversations = [
                        {
                            "name": c.name,
                            "streak_days": c.streak_days,
                            "kind": getattr(c, "kind", "unknown"),
                            "avatar_url": getattr(c, "avatar_url", ""),
                            "member_count": getattr(c, "member_count", None),
                        }
                        for c in rows
                    ]
                    metadata = conversation_metadata(all_conversations)
                    try:
                        save_conversations(
                            data.account_dir(account.name) / "conversations.json",
                            all_conversations,
                            datetime.now(settings.zone).isoformat(timespec="seconds"),
                        )
                    except OSError:
                        log.warning("会话展示信息缓存失败：%s", account.name)
                    if args.sparks_only:
                        rows = [c for c in rows if c.streak_days is not None]
                    # 火花天数落到账号目录，面板的对象列表随时可显示，不必再拉一次。
                    SparkRecord(
                        updated_at=datetime.now(settings.zone).isoformat(timespec="seconds"),
                        days={c.name: c.streak_days for c in rows if c.streak_days is not None},
                    ).save(data.sparks_file(account.name))
                    item["conversations"] = [
                        {
                            "name": c.name,
                            "streak_days": c.streak_days,
                            "kind": metadata.get(c.name, {}).get("kind", "unknown"),
                            "avatar_url": metadata.get(c.name, {}).get("avatar_url", ""),
                            "member_count": metadata.get(c.name, {}).get("member_count"),
                        }
                        for c in rows
                    ]
                    item["count"] = len(rows)
                    if not rows:
                        say("   （没有拉到会话，可能页面改版或列表为空）")
                    for conv in rows:
                        days = conv.streak_days
                        streak = f"  火花{days}天" if days is not None else ""
                        say(f"   - {conv.name}{streak}")
                    say(f"   共 {len(rows)} 个")
                    if args.save:
                        names_out = [c.name for c in rows]
                        try:
                            set_friends(config_path, name, names_out)
                        except FriendEditError as exc:
                            say(f"   写入失败：{exc}")
                            worst = max(worst, 1)
                        else:
                            say(f"   已写入 {config_path} friends（{len(names_out)} 个）")
                    session.snapshot("friends")
            except Exception as exc:
                log.debug("friends failed", exc_info=True)
                item["error"] = brief_exc(exc)
                item.update(
                    ok=False, health=health.record(name, "network_error", item["error"], **fields)
                )
                say(f"   失败：{item['error']}")
                worst = max(worst, 1)
            reporter.progress(reporter.data)
    if args.as_json:
        print(json.dumps({"accounts": payload}, ensure_ascii=True, indent=2))
    return worst
