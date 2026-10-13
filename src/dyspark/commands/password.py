"""Explicit offline panel password recovery without command-line secrets."""

from __future__ import annotations

import getpass
import secrets
import sys

from dyspark.config import Settings
from dyspark.gui_auth import (
    PASSWORD_FILE,
    PASSWORD_SERVICE_LOCK,
    AuthError,
    PasswordHash,
    save_password,
)
from dyspark.locking import LockBusyError, file_lock
from dyspark.store import ensure_private_dir


def cmd_password_reset(settings: Settings, *, random_password: bool = False) -> int:
    try:
        ensure_private_dir(settings.data_dir)
        with file_lock(settings.data_dir / PASSWORD_SERVICE_LOCK):
            if random_password:
                password = secrets.token_urlsafe(24)
            else:
                if not sys.stdin or not sys.stdin.isatty():
                    raise AuthError("请在交互终端执行 spark password reset，或使用 --random")
                password = getpass.getpass("新面板密码：")
                confirmation = getpass.getpass("确认新密码：")
                if password != confirmation:
                    raise AuthError("两次输入的新密码不一致")
            hashed = PasswordHash.create(password)
            save_password(settings.data_dir / PASSWORD_FILE, hashed)
        print("面板密码已重置，启动服务后使用新密码登录。")
        if random_password:
            print(f"新面板密码：{password}")
            print("仅显示本次，请妥善保存。")
        return 0
    except LockBusyError:
        print("面板服务正在运行，请先停止 spark gui，再执行密码重置。", file=sys.stderr)
        return 75
    except (AuthError, EOFError, KeyboardInterrupt) as exc:
        print(f"密码重置未完成：{exc or '输入已取消'}", file=sys.stderr)
        return 64
    except OSError as exc:
        print(f"密码重置失败：数据目录不可用（{type(exc).__name__}）", file=sys.stderr)
        return 73
