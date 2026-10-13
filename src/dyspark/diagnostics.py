"""Read-only environment checks for CLI, GUI and MCP installations."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from dyspark.config import BrowserOptions, SettingsError, read_settings
from dyspark.runtime import browser_executable, needs_display

# Keep these aligned with pyproject.toml. Optional features do not block CLI use.
DEPENDENCIES = (
    ("playwright", (1, 48), 2, "cli"),
    ("python-dotenv", (1, 0), 2, "cli"),
    ("segno", (1, 5), 2, "cli"),
    ("tzdata", (2024, 1), None, "cli"),
    ("starlette", (0, 37), 2, "gui"),
    ("uvicorn", (0, 30), 1, "gui"),
    ("httpx", (0, 27), 1, "gui"),
    ("tomlkit", (0, 12), 1, "cli"),
    ("mcp", (2, 3), 3, "mcp"),
)


def inspect_environment(
    config_path: Path, data_dir: Path | None = None, *, feature: str = "cli"
) -> dict[str, Any]:
    """Inspect prerequisites; never log in, send, create data directories or query IPs."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    add("python", sys.version_info >= (3, 11), f"{platform.python_version()}（需要 Python 3.11+）")
    add("platform", sys.platform in ("win32", "darwin", "linux"), platform.platform())
    for package, minimum, maximum, group in DEPENDENCIES:
        required = group == "cli" or group == feature
        try:
            installed = version(package)
        except PackageNotFoundError:
            add(package, False, "未安装", required=required)
            continue
        match = re.match(r"(\d+)\.(\d+)", installed)
        numbers = tuple(map(int, match.groups())) if match else (0, 0)
        valid = numbers >= minimum and (maximum is None or numbers[0] < maximum)
        detail = installed if valid else f"{installed}（版本不符合项目要求）"
        add(package, valid, detail, required=required)

    config_path = config_path.expanduser().resolve()
    options = BrowserOptions()
    headed = True
    root = data_dir or config_path.parent / "data"
    try:
        settings = read_settings(config_path, data_dir)
    except SettingsError as exc:
        add("config", False, str(exc))
    else:
        options, root = settings.browser, settings.data_dir
        headed = not settings.browser.headless or not settings.login.headless
        add("config", True, str(config_path))

    try:
        executable = browser_executable(options)
    except Exception as exc:
        add("browser", False, f"无法检查浏览器：{type(exc).__name__}")
    else:
        add(
            "browser",
            executable is not None,
            str(executable)
            if executable
            else (f"找不到 {options.channel}；安装浏览器或设置 browser.executable_path"),
        )
    if needs_display() and headed:
        wrapper = shutil.which("xvfb-run")
        xauth = shutil.which("xauth")
        add(
            "display",
            wrapper is not None and xauth is not None,
            (
                f"可用 {wrapper}；裸机 CLI 请用 xvfb-run -a spark …"
                if wrapper and xauth
                else "Linux 无 DISPLAY；需要图形会话或安装 xvfb 和 xauth"
            ),
        )
    elif needs_display():
        add("display", True, "运行与网页登录已配置无头模式；窗口/二维码/noVNC 模式仍需显示环境")
    else:
        add("display", True, "系统桌面或 DISPLAY 可用")

    root = root.expanduser().resolve()
    parent = root
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    add("data", writable, f"{root}（仅检查权限，不写入文件）")
    return {
        "ok": all(item["ok"] for item in checks if item["required"]),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "feature": feature,
        "checks": checks,
    }


def format_diagnostics(report: dict[str, Any]) -> str:
    lines = [f"环境检查：{report['platform']} / {report['architecture']} / {report['feature']}"]
    for check in report["checks"]:
        mark = "OK" if check["ok"] else "FAIL" if check["required"] else "OPTIONAL"
        lines.append(f"[{mark}] {check['name']}: {check['detail']}")
    lines.append("检查通过" if report["ok"] else "检查未通过，请处理 FAIL 项后重试")
    return "\n".join(lines)
