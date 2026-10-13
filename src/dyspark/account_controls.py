"""Validated account controls shared by the panel surfaces."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit

from dyspark.account_health import HealthStore
from dyspark.accounts import add_account, delete_account, update_account, validate_templates
from dyspark.config import Settings, read_config_text, read_settings, validate_account_name
from dyspark.messages import Quote, render
from dyspark.names import normalize_name
from dyspark.store import DataDir, LoginRecord, SparkRecord

_SAVED_TEMPLATES = object()


class AccountControlError(ValueError):
    """A requested state transition cannot safely be made."""


class AccountControls:
    def __init__(self, config_path: Path, data_dir: Path | None = None) -> None:
        self.path = config_path
        self.data_arg = data_dir

    def settings(self) -> Settings:
        return read_settings(self.path, self.data_arg)

    def metadata(self, settings: Settings) -> dict[str, dict[str, Any]]:
        doc = tomlkit.parse(read_config_text(self.path))
        rows = {row["name"]: row for row in doc.get("accounts", [])}
        health = HealthStore(settings.data_dir)
        result = {}
        for account in settings.accounts:
            raw_proxy = str(rows.get(account.name, {}).get("proxy", ""))
            proxy_ref = (
                raw_proxy if re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", raw_proxy) else ""
            )
            state = health.view(account.name)
            result[account.name] = {
                "enabled": account.enabled,
                "templates": list(account.templates),
                "proxy_ref": proxy_ref,
                "proxy_editable": not raw_proxy or bool(proxy_ref),
                "health": state,
                "identity_verified": False,
            }
        return result

    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        if "name" not in payload or (
            isinstance(payload["name"], str) and not payload["name"].strip()
        ):
            payload["name"] = "account_" + secrets.token_hex(6)
        if isinstance(payload.get("name"), str):
            name = validate_account_name(payload["name"].strip())
            existing = self.settings()
            if not any(a.name.casefold() == name.casefold() for a in existing.accounts):
                folder = existing.data_dir / "accounts" / name
                if folder.exists() or folder.is_symlink():
                    raise AccountControlError(
                        "此标识已有保留资料，请使用新的账号标识，避免混用登录和历史记录"
                    )
        settings = add_account(self.path, payload)
        account = settings.account(payload["name"].strip())
        return {"account": account.name, "message": "账号已添加；扫码登录成功后会自动启用"}

    def delete(self, name: str) -> dict[str, Any]:
        delete_account(self.path, name)
        return {
            "account": name,
            "message": "账号已从面板删除；登录资料和历史记录已保留，不再参与发送",
        }

    def update(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        account = settings.account(name)
        if payload.get("enabled") is True:
            raise AccountControlError("启用账号请使用启用操作（账号开关）")
        # 用户意愿只写 config：停用不动健康记录，诊断信息留给下一次现场核对更新。
        updated = update_account(self.path, name, payload).account(name)
        if updated.proxy != account.proxy:
            HealthStore(settings.data_dir).pause(
                name, "代理配置已变化，请重新扫码登录", "ip_mismatch"
            )
        return {
            "account": name,
            "message": "账号已停用，后续对象不会发送；设置和登录资料保留"
            if payload == {"enabled": False}
            else "账号配置已保存",
        }

    def resume(self, name: str) -> dict[str, Any]:
        """Enable an account for automatic sending. The switch only decides whether the
        account takes part; every real send still re-verifies the session live, so no
        fresh check, IP comparison or target list is needed to turn it back on."""
        settings = self.settings()
        account = settings.account(name)
        record = LoginRecord.load(DataDir(settings.data_dir).login_file(account.name))
        if record is None:
            raise AccountControlError("此账号还没有登录记录，请先扫码登录")
        update_account(self.path, name, {"enabled": True})
        HealthStore(settings.data_dir).enable(name)
        return {"account": name, "message": "账号已启用；自动运行还需启用每日计划"}

    def preview(
        self, name: str, friend: object = None, *, templates: object = _SAVED_TEMPLATES
    ) -> dict[str, Any]:
        settings = self.settings()
        account = settings.account(name)
        if friend is not None and (not isinstance(friend, str) or len(friend) > 64):
            raise AccountControlError("friend 必须是 64 字以内的名称")
        target = (friend.strip() if isinstance(friend, str) else "") or next(
            iter(account.friends), "好友"
        )
        candidates = (
            account.templates if templates is _SAVED_TEMPLATES else validate_templates(templates)
        )
        source = "account" if candidates else "global"
        candidates = candidates or settings.messages.templates
        try:
            sparks = SparkRecord.load(DataDir(settings.data_dir).sparks_file(name))
        except OSError:
            sparks = None
        cached = {
            normalize_name(key): value
            for key, value in (sparks.days if sparks else {}).items()
            if value >= 0
        }
        days = cached.get(normalize_name(target))
        today = datetime.now(settings.zone).date()
        quote = Quote(settings.messages.hitokoto_fallback)
        return {
            "account": name,
            "friend": target,
            "template_source": source,
            "messages": [
                render(
                    t,
                    today=today,
                    friend=target,
                    days=days,
                    quote=quote,
                )
                for t in candidates
            ],
            "note": "本账号下所有发送对象共用此模板规则，每次发送随机选取一条。"
            + (
                "内置每日一言按日期轮换，同一天预览与发送使用相同短句。"
                if any("{daily_quote}" in t for t in candidates)
                else ""
            )
            + (
                "一言预览使用回退文本，来源留空。"
                if any("{hitokoto}" in t or "{source}" in t for t in candidates)
                else ""
            ),
        }
