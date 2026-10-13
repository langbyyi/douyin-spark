"""Validated account edits that preserve TOML comments and environment references."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import AoT, Array, Table

from dyspark.config import (
    Account,
    Settings,
    SettingsError,
    read_settings,
    validate_account_name,
    validate_account_remark,
)
from dyspark.config_edit import ConfigEditError, edit_config
from dyspark.friends import clean_name
from dyspark.store import DataDir, LoginRecord, read_json

_PROXY_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_PROXY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EDITABLE = frozenset({"enabled", "friends", "templates", "proxy", "remark"})
MAX_FRIENDS = 500
MAX_TEMPLATES = 100
MAX_TEMPLATE_LENGTH = 2000
MAX_PROXY_REFERENCE_LENGTH = 128


def validate_proxy_reference(proxy: object) -> str:
    """Normalize an account proxy entry into a ``${VAR}`` reference.

    Accepts the bare variable name (``MAIN_PROXY``) or the full reference
    (``${MAIN_PROXY}``); empty means direct connection. Only the reference is
    stored in config — the proxy URL and its credentials live in ``.env``.
    """
    if not isinstance(proxy, str):
        raise SettingsError("代理名称必须是文本")
    proxy = proxy.strip()
    matched = _PROXY_REF.fullmatch(proxy)
    if matched:
        proxy = matched.group(1)
    if not proxy:
        return ""
    if len(proxy) > MAX_PROXY_REFERENCE_LENGTH or not _PROXY_NAME.fullmatch(proxy):
        raise SettingsError(
            "代理名称只支持字母、数字和下划线（如 MAIN_PROXY）；代理地址与密码请写在 .env 中"
        )
    return f"${{{proxy}}}"


def _name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ConfigEditError("账号名只能包含字母、数字、_ 和 -（1~32 个字符）")
    try:
        return validate_account_name(raw.strip())
    except SettingsError as exc:
        raise ConfigEditError(str(exc)) from exc


def _string_list(raw: object, key: str, maximum: int) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigEditError(f"{key} 必须是字符串列表")
    if len(raw) > maximum:
        raise ConfigEditError(f"{key} 最多 {maximum} 项")
    return raw


def validate_templates(raw: object) -> list[str]:
    templates = _string_list(raw, "templates", MAX_TEMPLATES)
    for template in templates:
        if not template.strip() or len(template) > MAX_TEMPLATE_LENGTH:
            raise ConfigEditError(f"templates 每项须为 1~{MAX_TEMPLATE_LENGTH} 个字符")
        if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in template):
            raise ConfigEditError("templates 不能包含异常控制字符")
    return [template.strip() for template in templates]


def _payload(raw: object, *, adding: bool) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ConfigEditError("账号配置必须是字段名为字符串的对象")
    allowed = _EDITABLE | {"name"} if adding else _EDITABLE
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigEditError(f"不可编辑的账号配置项：{', '.join(unknown)}")
    values = dict(raw)
    if "remark" in values:
        try:
            values["remark"] = validate_account_remark(values["remark"])
        except SettingsError as exc:
            raise ConfigEditError(str(exc)) from exc
    if adding:
        values["name"] = _name(raw.get("name"))
        values.setdefault("enabled", False)
        values.setdefault("friends", [])
    if "enabled" in values and not isinstance(values["enabled"], bool):
        raise ConfigEditError("enabled 必须是 true 或 false")
    if "friends" in values:
        values["friends"] = [
            clean_name(item) for item in _string_list(values["friends"], "friends", MAX_FRIENDS)
        ]
    if "templates" in values:
        values["templates"] = validate_templates(values["templates"])
    if "proxy" in values:
        try:
            values["proxy"] = validate_proxy_reference(values["proxy"])
        except SettingsError as exc:
            raise ConfigEditError(str(exc)) from exc
    return values


def discard_unregistered_profile(
    config_path: Path, data_dir: Path, name: str, *, owner: str | None = None
) -> bool:
    """回收一次「没登记成账号」的登录留下的浏览器资料，返回是否真的删掉了。

    登记前生成的 owner 只允许回收该次尝试创建的目录，包括已写出登录记录的失败尝试。
    不带 owner 时保留有登录记录的目录；配置读取失败或路径越界时不删除。
    """
    if not isinstance(name, str):
        return False
    try:
        validate_account_name(name)
        registered = {a.name.casefold() for a in read_settings(config_path, data_dir).accounts}
    except (SettingsError, OSError):
        return False  # 配置读不出来就一律不删
    root = Path(data_dir).resolve()
    directory = root / "accounts" / name
    if (
        name.casefold() in registered
        or not directory.is_dir()
        or directory.is_symlink()
        or directory.resolve().parent != root / "accounts"
    ):
        return False
    if owner is not None:
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
            return False
        marker = read_json(directory / ".pending-registration.json", {})
        if not isinstance(marker, dict) or marker.get("owner") != owner:
            return False
    elif (directory / "login.json").exists():
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return not directory.exists()


def find_duplicate_account(
    config_path: Path, data_dir: Path, user_id: str
) -> tuple[Account, str] | None:
    """按抖音用户标识找已登记的同人账号，返回 (账号, 展示名) 供去重提示使用。

    只比对写进了 user_id 的登录记录；旧记录没有这个字段时不参与去重，
    「检查账号」会把它补上。
    """
    if not isinstance(user_id, str) or not user_id:
        return None
    settings = read_settings(config_path, data_dir)
    data = DataDir(data_dir)
    for existing in settings.accounts:
        previous = LoginRecord.load(data.login_file(existing.name))
        if previous is None or previous.user_id != user_id:
            continue
        return existing, existing.remark or previous.nickname or existing.name
    return None


def add_account(path: Path, payload: dict) -> Settings:
    """Add an account, disabled by default; a successful login turns it on."""
    values = _payload(payload, adding=True)

    def change(doc: tomlkit.TOMLDocument) -> None:
        accounts = doc.get("accounts")
        if accounts is None or (isinstance(accounts, Array) and not accounts):
            accounts = tomlkit.aot()
            doc["accounts"] = accounts
        if not isinstance(accounts, AoT):
            raise ConfigEditError("accounts 必须写成 [[accounts]] 表数组")
        if any(
            str(table.get("name", "")).strip().casefold() == values["name"].casefold()
            for table in accounts
        ):
            raise ConfigEditError("账号名 name 不能重复（不区分大小写）")
        table = tomlkit.table()
        for key, value in values.items():
            table[key] = value
        accounts.append(table)

    return edit_config(path, change)


def delete_account(path: Path, name: str) -> Settings:
    """Remove only the configuration; profiles and historic results remain intact."""
    account_name = _name(name)

    def change(doc: tomlkit.TOMLDocument) -> None:
        accounts = doc.get("accounts")
        if not isinstance(accounts, AoT):
            raise ConfigEditError(f"未知账号 {account_name!r}")
        for index, table in enumerate(accounts):
            if table.get("name") == account_name:
                del accounts[index]
                if not accounts:
                    del doc["accounts"]
                    doc["accounts"] = []
                    if "schedule" not in doc:
                        doc["schedule"] = tomlkit.table()
                    doc["schedule"]["enabled"] = False
                return
        raise ConfigEditError(f"未知账号 {account_name!r}")

    return edit_config(path, change)


def update_account(path: Path, name: str, payload: dict) -> Settings:
    """Edit explicit fields; account identifiers and unrelated settings stay intact."""
    account_name = _name(name)
    values = _payload(payload, adding=False)

    def change(doc: tomlkit.TOMLDocument) -> None:
        accounts = doc.get("accounts")
        if isinstance(accounts, AoT):
            for table in accounts:
                if isinstance(table, Table) and table.get("name") == account_name:
                    for key, value in values.items():
                        table[key] = value
                    return
        raise ConfigEditError(f"未知账号 {account_name!r}")

    return edit_config(path, change)
