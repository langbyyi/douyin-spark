"""Edit an account's ``friends`` list in config.toml, keeping comments and layout.

Every edit is validated by parsing the whole new configuration before it is
written, so a tool call can never leave a config that ``spark run`` rejects.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Array, Table

from dyspark.chat import normalize_name
from dyspark.config_edit import ConfigEditError as FriendEditError
from dyspark.config_edit import edit_config
from dyspark.config_edit import write_config as write_config

MAX_NAME_LENGTH = 64


def clean_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise FriendEditError("对象名称必须是文字（好友备注名、昵称或群名）")
    name = raw.strip()
    if not name:
        raise FriendEditError("对象名称不能为空")
    if len(name) > MAX_NAME_LENGTH:
        raise FriendEditError(f"对象名称最长 {MAX_NAME_LENGTH} 个字符")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise FriendEditError("对象名称不能包含换行或控制字符")
    return name


def add_friend(config_path: Path, account: str, raw_name: object) -> list[str]:
    name = clean_name(raw_name)

    def change(friends: Array) -> None:
        wanted = normalize_name(name)
        if any(normalize_name(str(f)) == wanted for f in friends):
            raise FriendEditError(f"对象 {name} 已在账号 {account} 的列表中")
        friends.append(name)

    return _edit(config_path, account, change)


def remove_friend(config_path: Path, account: str, raw_name: object) -> list[str]:
    name = clean_name(raw_name)

    def change(friends: Array) -> None:
        current = [str(f) for f in friends]
        if name in current:
            index = current.index(name)
        else:
            wanted = normalize_name(name)
            matches = [i for i, f in enumerate(current) if normalize_name(f) == wanted]
            if not matches:
                raise FriendEditError(f"账号 {account} 的对象列表中没有 {name}")
            index = matches[0]
        del friends[index]

    return _edit(config_path, account, change)


def set_friends(config_path: Path, account: str, raw_names: object) -> list[str]:
    """Replace the whole friends list (validated names; empty list allowed)."""
    if not isinstance(raw_names, list):
        raise FriendEditError("对象名单必须是数组")
    cleaned = [clean_name(n) for n in raw_names]
    # Deduplicate while preserving order (NFKC, no casefold).
    seen: set[str] = set()
    unique: list[str] = []
    for name in cleaned:
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)

    def change(friends: Array) -> None:
        friends.clear()
        for name in unique:
            friends.append(name)

    return _edit(config_path, account, change)


def _edit(config_path: Path, account: str, change: Callable[[Array], None]) -> list[str]:
    def update(doc: tomlkit.TOMLDocument) -> None:
        table = _account_table(doc, account)
        friends = table.get("friends")
        if friends is None:
            friends = tomlkit.array()
            table["friends"] = friends
        if not isinstance(friends, Array):
            raise FriendEditError(f"账号 {account} 的 friends 必须是数组")
        change(friends)

    settings = edit_config(config_path, update)
    return list(settings.account(account).friends)


def _account_table(doc: tomlkit.TOMLDocument, account: str) -> Table:
    accounts = doc.get("accounts")
    if isinstance(accounts, AoT | list):
        for table in accounts:
            if isinstance(table, Table | dict) and table.get("name") == account:
                return table
    raise FriendEditError(f"未知账号 {account!r}")
