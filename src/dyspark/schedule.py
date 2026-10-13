"""Describe configured run times and the project service that executes them."""

from __future__ import annotations

import platform
import re
import shlex
import sys
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from dyspark.config import Settings
from dyspark.config_edit import ConfigEditError, edit_config
from dyspark.schedule_time import due_at


def set_times(config_path: Path, raw_times: object) -> list[str]:
    """Save user-defined daily times while preserving unrelated configuration."""
    import tomlkit

    if not isinstance(raw_times, list) or not raw_times:
        raise ConfigEditError("请至少设置一个每日时间，times 必须是数组")
    if any(
        not isinstance(t, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t)
        for t in raw_times
    ):
        raise ConfigEditError("每日时间必须是 HH:MM 格式，如 09:05")
    times = list(dict.fromkeys(raw_times))

    def update(doc: tomlkit.TOMLDocument) -> None:
        if "schedule" not in doc:
            doc["schedule"] = tomlkit.table()
        doc["schedule"]["times"] = times

    return list(edit_config(config_path, update).schedule_times)


def next_fire_times(
    times: tuple[str, ...],
    zone: tzinfo,
    *,
    now: datetime | None = None,
    count: int = 4,
) -> list[str]:
    """Upcoming HH:MM firings in ``zone`` (including later today)."""
    now = now or datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    now = now.astimezone(zone)
    today = now.date()
    parsed: list[tuple[int, int]] = []
    for item in times:
        hour, minute = item.split(":")
        parsed.append((int(hour), int(minute)))
    parsed = sorted(set(parsed))
    if not parsed:
        return []
    out: list[str] = []
    day = today
    while len(out) < count:
        for hour, minute in parsed:
            candidate = due_at(day, f"{hour:02d}:{minute:02d}", zone)
            if candidate is not None and candidate.timestamp() >= now.timestamp():
                out.append(candidate.isoformat(timespec="minutes"))
                if len(out) >= count:
                    break
        day = day + timedelta(days=1)
    return out


def describe(settings: Settings, config_path: Path) -> dict[str, Any]:
    zone = settings.zone
    now = datetime.now(zone)
    times = list(settings.schedule_times)
    next_runs = next_fire_times(tuple(times), zone, now=now)
    system = platform.system()
    argv = [*_spark_argv(config_path), "--data", str(settings.data_dir.resolve())]
    return {
        "times": times,
        "timezone": str(zone),
        "now": now.isoformat(timespec="seconds"),
        "next_runs": next_runs,
        "cloud": settings.cloud,
        "platform": system,
        "spark_command": _quote_command(argv),
        "mcp": {"command": argv[0], "args": [*argv[1:], "mcp"]},
        "service": {
            "command": f"{_quote_command(argv)} gui",
            "note": "关闭网页不影响服务；停止服务会停止调度并取消其后台任务。",
        },
        "docs": {
            "mcp": "docs/mcp.md",
            "readme_cloud": "docs/cloud.md",
            "readme_gui": "docs/usage.md",
        },
    }


def _spark_argv(config_path: Path) -> list[str]:
    exe = Path(sys.executable)
    # Prefer the console script next to this interpreter. Do not resolve() the
    # executable first: a venv's python is often a symlink into /usr/bin, and
    # resolving would look for `spark` beside the system interpreter.
    for base in (exe.parent, exe.resolve().parent):
        for name in ("spark", "spark.exe"):
            candidate = base / name
            if candidate.is_file():
                return [str(candidate), "--config", str(config_path.resolve())]
    return [str(exe), "-m", "dyspark", "--config", str(config_path.resolve())]


def _quote_command(argv: list[str]) -> str:
    if platform.system() == "Windows":
        # Displayed in PowerShell, where & invokes a quoted executable path.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def print_schedule(info: dict[str, Any]) -> str:
    lines = [
        f"计划时间（{info['timezone']}）：{', '.join(info['times'])}",
        f"当前：{info['now']}",
        "配置中的下次计划时间（实际执行取决于开关、服务和账号状态）：",
    ]
    for item in info["next_runs"]:
        lines.append(f"  - {item}")
    lines.extend(
        [
            "",
            "启动项目服务（spark gui）：",
            f"  {info['service']['command']}",
            info["service"]["note"],
        ]
    )
    return "\n".join(lines)
