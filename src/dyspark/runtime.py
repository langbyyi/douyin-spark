"""Platform knowledge shared by browser startup, jobs and environment diagnostics."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dyspark.config import BrowserOptions


def needs_display() -> bool:
    """Linux headed Chromium needs X11; macOS and Windows use native desktops."""
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


def supports_window_position() -> bool:
    return sys.platform != "darwin"


def browser_executable(options: BrowserOptions) -> Path | None:
    """Find the configured browser without starting it or changing the profile."""
    if options.executable_path:
        path = Path(options.executable_path).expanduser().resolve()
        return path if path.is_file() else None
    if options.channel == "chromium":
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            path = Path(playwright.chromium.executable_path)
        return path if path.is_file() else None

    edge = options.channel == "msedge"
    if sys.platform == "win32":
        suffix = (
            "Microsoft/Edge/Application/msedge.exe"
            if edge
            else "Google/Chrome/Application/chrome.exe"
        )
        paths = [
            Path(base) / suffix
            for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (base := os.environ.get(key))
        ]
    elif sys.platform == "darwin":
        app, binary = (
            ("Microsoft Edge", "Microsoft Edge") if edge else ("Google Chrome", "Google Chrome")
        )
        suffix = f"{app}.app/Contents/MacOS/{binary}"
        paths = [Path("/Applications") / suffix, Path.home() / "Applications" / suffix]
    else:
        names = (
            ("microsoft-edge", "microsoft-edge-stable")
            if edge
            else ("google-chrome", "google-chrome-stable")
        )
        paths = [Path(found) for name in names if (found := shutil.which(name))]
        paths.append(Path("/opt/microsoft/msedge/msedge" if edge else "/opt/google/chrome/chrome"))
    return next((path for path in paths if path.is_file()), None)
