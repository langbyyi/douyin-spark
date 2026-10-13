"""One-shot run command: locking, progress, summaries and retention."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from dyspark.config import Settings, read_settings
from dyspark.jobs import JobReporter
from dyspark.notify import Notifier
from dyspark.store import DataDir, RunBusyError, exclusive_run, purge_old_files, write_json_atomic

log = logging.getLogger("dyspark")


def cmd_run(
    settings: Settings, args: argparse.Namespace, run_dir: Path, reporter: JobReporter
) -> int:
    from dyspark.runner import AccountReport, Runner
    from dyspark.session import live_session

    data = DataDir(settings.data_dir)
    notifier = Notifier(settings.notify, state_path=settings.data_dir / "notify-state.json")
    started = datetime.now(settings.zone).isoformat(timespec="seconds")
    base: dict[str, Any] = {"run": run_dir.name, "dry_run": args.dry_run, "started_at": started}

    def progress(reports: list[AccountReport]) -> None:
        reporter.progress({**base, "accounts": [r.as_dict() for r in reports]})

    def is_enabled(name: str) -> bool:
        current = read_settings(args.config, settings.data_dir)
        return any(account.name == name and account.enabled for account in current.accounts)

    try:
        with exclusive_run(data.lock_file):
            runner = Runner(
                settings,
                partial(live_session, settings, run_dir),
                notifier,
                on_progress=progress,
                is_enabled=is_enabled,
            )
            code = runner.run(args.accounts, dry_run=args.dry_run, jitter=not args.now)
    except RunBusyError as exc:
        log.warning("%s；本次触发跳过", exc)
        reporter.data = {**base, "busy": True, "message": str(exc)}
        return 0
    summary = {
        **base,
        "finished_at": datetime.now(settings.zone).isoformat(timespec="seconds"),
        "exit_code": code,
        "trigger": args.trigger,
        "accounts": [r.as_dict() for r in runner.reports],
    }
    reporter.data = summary
    try:
        write_json_atomic(data.last_run_file, summary)
    except OSError as exc:
        log.warning("could not write %s: %s", data.last_run_file, exc)
    try:
        purge_old_files(data, datetime.now(settings.zone).date(), settings.keep_days)
    except OSError as exc:
        log.warning("cleanup failed: %s", exc)
    return code
