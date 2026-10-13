"""Install into a project-local virtual environment on Windows, macOS or Linux.

Run with Python 3.11+: python deploy/install.py --extras gui mcp
Only --browser chromium downloads a browser; no system scheduler is installed.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path


def initialize_config(root: Path, *, chromium: bool = False) -> list[Path]:
    """Create missing config files exclusively; never replace existing user files."""
    created = []
    for source, name in (("config.example.toml", "config.toml"), (".env.example", ".env")):
        text = (root / source).read_text(encoding="utf-8-sig")
        if chromium and name == "config.toml":
            text = text.replace('channel = "chrome"', 'channel = "chromium"', 1)
        target = root / name
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except FileExistsError:
            continue
        created.append(target)
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install dyspark in .venv without system changes")
    parser.add_argument(
        "--extras", nargs="+", choices=["cli", "gui", "mcp", "dev"], default=["gui"]
    )
    parser.add_argument("--browser", choices=["chromium"], help="Download Playwright Chromium")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):  # noqa: UP036 - report unsupported installer interpreters
        print("Python 3.11+ is required.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if environment.exists() and not (environment / "pyvenv.cfg").is_file():
            raise ValueError(
                ".venv exists but is not a virtual environment; choose a clean project directory"
            )
        if not python.is_file():
            print(f"Creating virtual environment: {environment}", flush=True)
            venv.create(environment, with_pip=True)
        extras = sorted(set(args.extras) - {"cli"})
        package = str(root) + (f"[{','.join(extras)}]" if extras else "")
        print("Installing project dependencies in .venv ...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-e", package], cwd=root, check=True)
        if args.browser:
            subprocess.run(
                [str(python), "-m", "playwright", "install", "chromium"], cwd=root, check=True
            )
        created = initialize_config(root, chromium=args.browser == "chromium")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(f"Created: {path}")
    if args.browser and root / "config.toml" not in created:
        print('Existing config preserved; set [browser] channel = "chromium" to use the download.')
    if os.name == "nt":
        command = (
            "& '"
            + str(python).replace("'", "''")
            + "' -m dyspark --config '"
            + str(root / "config.toml").replace("'", "''")
            + "'"
        )
    else:
        command = shlex.join([str(python), "-m", "dyspark", "--config", str(root / "config.toml")])
    print(f"Edit config.toml, then check your environment:\n  {command} doctor")
    if "gui" in extras or "dev" in extras:
        print(f"Open the local panel:\n  {command} gui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
