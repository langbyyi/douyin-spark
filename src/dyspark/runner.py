"""One scheduled invocation: for each account, verify the environment, then send.

The runner only talks to a :class:`ChatSession`, so the whole decision flow can
be tested without a browser.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dyspark.account_health import HealthStore
from dyspark.account_profile import OwnProfile
from dyspark.chat import PageState, SendGuardError
from dyspark.config import Account, Settings
from dyspark.delivery_failure import failure_info
from dyspark.live_check import check_live_environment
from dyspark.logs import brief_exc
from dyspark.messages import MessageFactory
from dyspark.names import normalize_name
from dyspark.notify import Notifier
from dyspark.receipt import Outcome, Receipt
from dyspark.store import (
    NEED_LOGIN_NOTE,
    PROXY_CHANGED_NOTE,
    DataDir,
    Ledger,
    LoginRecord,
    RunBusyError,
    SparkRecord,
    proxy_fingerprint,
)

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 2


class _OpenFailure(Receipt):
    """Conversation selection failed before typing; no sending gap is needed."""

    __slots__ = ()


NOT_FOUND = _OpenFailure(Outcome.FRIEND_ERROR, "找不到会话")


class _AccountStopped(Exception):
    """A pause observed immediately before send; nothing was sent/reserved."""


class _FriendSettled(Exception):
    """The current day's ledger already completes or holds this conversation."""


class ChatSession(Protocol):
    def egress_ip(self) -> str: ...
    def page_state(self) -> PageState: ...
    def current_page_state(self) -> PageState: ...
    def own_profile(self) -> OwnProfile | None: ...
    def open_friend(self, name: str) -> bool: ...
    def send(self, text: str) -> Receipt: ...
    def snapshot(self, label: str) -> None: ...


SessionFactory = Callable[[Account], AbstractContextManager[ChatSession]]


class Verdict(StrEnum):
    """Per-account result, ordered by severity."""

    DONE = "done"
    PARTIAL = "partial"
    ERROR = "error"
    IP_MISMATCH = "ip_mismatch"
    NEEDS_LOGIN = "needs_login"
    BLOCKED = "blocked"  # captcha / risk control

    @property
    def severity(self) -> int:
        return list(Verdict).index(self)

    @property
    def exit_code(self) -> int:
        """0 ok, 1 partial/error, 2 login or IP problem, 3 risk control."""
        return {"done": 0, "partial": 1, "error": 1, "ip_mismatch": 2, "needs_login": 2}.get(
            self.value, 3
        )


VERDICT_LABEL = {
    Verdict.DONE: "成功",
    Verdict.PARTIAL: "发送失败",
    Verdict.ERROR: "执行失败",
    Verdict.IP_MISMATCH: "发送失败（出口 IP 变化）",
    Verdict.NEEDS_LOGIN: "发送失败（登录失效）",
    Verdict.BLOCKED: "发送失败（安全验证）",
}


@dataclass(slots=True)
class AccountReport:
    account: str
    verdict: Verdict = Verdict.DONE
    note: str = ""
    sent: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)  # per friend, in order
    already_sent: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        data["exit_code"] = self.verdict.exit_code
        for item in data["results"]:
            if item.get("outcome") in {
                "failed",
                "unconfirmed",
                "sending",
                "friend_error",
                "risk",
                "logged_out",
            }:
                item["failure"] = failure_info(
                    item["outcome"],
                    item.get("reason", ""),
                    held=bool(item.get("held")),
                    retry_exhausted=bool(item.get("retry_exhausted")),
                )
        return data

    def line(self) -> str:
        parts = [f"[{self.account}] {VERDICT_LABEL[self.verdict]}"]
        if self.note:
            parts.append(self.note)
        if self.sent:
            parts.append("已发: " + "、".join(self.sent))
        if self.already_sent:
            parts.append("今日已发送，本次跳过: " + "、".join(self.already_sent))
        previous = [
            item["friend"] for item in self.results if item.get("outcome") == "previous_day_sent"
        ]
        if previous:
            parts.append("跨日前已发送，本次跳过: " + "、".join(previous))
        if self.failed:
            rows = {item.get("friend"): item for item in self.results}
            reasons = {
                name: failure_info(rows.get(name, {}).get("outcome", "failed"), reason)["reason"]
                for name, reason in self.failed.items()
            }
            parts.append(
                "失败: " + "；".join(f"{name}({reason})" for name, reason in reasons.items())
            )
        return " | ".join(parts)


class Runner:
    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        notifier: Notifier,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        on_progress: Callable[[list[AccountReport]], None] | None = None,
        is_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.data = DataDir(settings.data_dir)
        self.health = HealthStore(settings.data_dir)
        self.session_factory = session_factory
        self.notifier = notifier
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(ZoneInfo(settings.timezone)))
        self.on_progress = on_progress
        self.is_enabled = is_enabled
        self.reports: list[AccountReport] = []  # of the latest run(), for callers and tools

    def _reconcile_completed(self, reports: list[AccountReport]) -> None:
        today = self.now().date().isoformat()
        for report in reports:
            prior_names = set()
            for item in report.results:
                sent_date = item.get("sent_date")
                if item.get("outcome") != "already_sent" or not sent_date or sent_date == today:
                    continue
                item["outcome"] = "previous_day_sent"
                item["reason"] = (
                    f"{sent_date} 已发送，本次按跨日前记录跳过；跨日后的发送由后续任务处理。"
                )
                prior_names.add(normalize_name(item["friend"]))
            report.already_sent[:] = [
                friend
                for friend in report.already_sent
                if normalize_name(friend) not in prior_names
            ]

    def _progress(self) -> None:
        self._reconcile_completed(self.reports)
        if self.on_progress is None:
            return
        try:
            self.on_progress(self.reports)
        except Exception:  # reporting must never disturb sending
            log.debug("progress callback failed", exc_info=True)

    # ------------------------------------------------------------------ public
    def run(
        self, names: Iterable[str] | None = None, *, dry_run: bool = False, jitter: bool = True
    ) -> int:
        accounts = [self.settings.account(n) for n in names] if names else self.settings.accounts
        ledger = Ledger(self.data.ledger_file(self.now().date()))
        if ledger.blocked:
            self.reports = [
                AccountReport(
                    a.name, Verdict.ERROR, "发送账本异常（含历史记录），已停止发送，请人工核对"
                )
                for a in accounts
                if a.enabled
            ]
            self._progress()
            if self.reports:
                self._report(self.reports, dry_run)
                return 1
            return 0
        active = [a for a in accounts if a.enabled and a.friends]
        self.reports = []
        if not active:
            log.info("nothing left to send today")
            return 0

        reports: dict[str, AccountReport] = {}
        ready: list[tuple[Account, LoginRecord]] = []
        for account in active:
            if not ledger.pending(account.name, list(account.friends)):
                report, record = AccountReport(account.name), None
            else:
                report, record = self._preflight(account)
            self._add_completed(account, ledger, report, list(account.friends))
            self._add_held(account, ledger, report, list(account.friends))
            reports[account.name] = report
            if record is not None:
                ready.append((account, record))
        self.reports = [reports[a.name] for a in active]
        self._progress()
        if ready and jitter and not dry_run and self.settings.timing.jitter_minutes > 0:
            delay = self.rng.uniform(0, self.settings.timing.jitter_minutes * 60)
            log.info("random start delay %.0fs", delay)
            self.sleep(delay)
        for account, record in ready:
            self._launch(account, record, ledger, reports[account.name], dry_run)
            self._progress()

        self._report(self.reports, dry_run)
        return max(r.verdict.exit_code for r in self.reports)

    def _preflight(self, account: Account) -> tuple[AccountReport, LoginRecord | None]:
        try:
            return self._preflight_account(account)
        except (OSError, RunBusyError) as exc:
            note = f"账号状态无法读取或保存，已停止该账号：{brief_exc(exc)}"
            self._pause_safely(account.name, note)
            return AccountReport(account.name, Verdict.ERROR, note), None

    def _preflight_account(self, account: Account) -> tuple[AccountReport, LoginRecord | None]:
        """Checks that need no browser; a ``None`` record means the account is skipped."""
        report = AccountReport(account.name)
        if not self._allowed(account, report):
            return report, None
        record = LoginRecord.load(self.data.login_file(account.name))
        if record is None:
            note = NEED_LOGIN_NOTE
            self.health.pause(account.name, note, "logged_out")
            self._fail(report, Verdict.NEEDS_LOGIN, note)
            return report, None
        if record.proxy_fingerprint != proxy_fingerprint(account.proxy):
            note = PROXY_CHANGED_NOTE
            self.health.record(
                account.name,
                "ip_mismatch",
                note,
                login_ip=record.egress_ip,
                proxy_fingerprint=proxy_fingerprint(account.proxy),
            )
            self._fail(report, Verdict.IP_MISMATCH, note)
            return report, None
        if self.settings.cloud and not account.proxy:
            log.warning(
                "[%s] cloud run without proxy: datacenter IPs are often risk-controlled",
                account.name,
            )
        return report, record

    def _launch(
        self,
        account: Account,
        record: LoginRecord,
        ledger: Ledger,
        report: AccountReport,
        dry_run: bool,
    ) -> None:
        try:
            with self.session_factory(account) as session:
                self._run_session(account, session, record, ledger, report, dry_run)
        except Exception as exc:
            log.exception("[%s] run crashed", account.name)
            self._pause_safely(account.name, f"异常: {brief_exc(exc)}")
            self._fail(report, Verdict.ERROR, f"异常: {brief_exc(exc)}")

    def _pause_safely(self, name: str, reason: str) -> None:
        try:
            self.health.pause(name, reason, "network_error")
        except (OSError, RunBusyError):
            log.exception("[%s] could not persist pause; account is stopped for this run", name)

    # ----------------------------------------------------------------- internals
    def _run_session(
        self,
        account: Account,
        session: ChatSession,
        record: LoginRecord,
        ledger: Ledger,
        report: AccountReport,
        dry_run: bool,
    ) -> None:
        current_path = self.data.ledger_file(self.now().date())
        if ledger.path != current_path:
            ledger = Ledger(current_path)
            old_holds = {item["friend"] for item in report.results if item.get("held")}
            report.results = [item for item in report.results if not item.get("held")]
            report.results = [
                item
                for item in report.results
                if item.get("outcome") not in {"already_sent", "previous_day_sent"}
            ]
            report.already_sent.clear()
            for friend in old_holds:
                report.failed.pop(friend, None)
            if old_holds and report.verdict is Verdict.PARTIAL and not report.failed:
                report.verdict = Verdict.DONE
                report.note = ""
        if ledger.blocked:
            self._fail(report, Verdict.ERROR, "发送账本异常（含历史记录），已停止发送，请人工核对")
            return
        if not self._allowed(account, report) or not self._check_live(
            account, session, record, report
        ):
            return

        streaks = self._cached_streaks(account)
        names: dict[str, str] = {}
        for name in account.friends:
            names.setdefault(normalize_name(name), name)
        targets = list(names.values())
        streaks = {normalize_name(name): days for name, days in streaks.items()}
        self._add_completed(account, ledger, report, targets)
        self._add_held(account, ledger, report, targets)
        pending = ledger.pending(account.name, targets)
        log.info("[%s] %d target(s), %d pending", account.name, len(targets), len(pending))
        messages = MessageFactory(self.settings.messages, account.templates, rng=self.rng)

        broken = 0
        paced = False  # the anti-risk gap only follows a (possible) real send

        def send_ledger() -> Ledger:
            nonlocal ledger
            path = self.data.ledger_file(self.now().date())
            if ledger.path != path:
                ledger = Ledger(path)
            if ledger.blocked:
                self._fail(
                    report, Verdict.ERROR, "发送账本异常（含历史记录），已停止发送，请人工核对"
                )
                raise _AccountStopped
            return ledger

        for friend in pending:
            if paced:
                self.sleep(self.rng.uniform(*self.settings.timing.gap_seconds))
            if not self._allowed(account, report):
                return
            if not dry_run and not self._check_live(account, session, record, report):
                return
            try:
                result = self._deliver(
                    account,
                    session,
                    messages,
                    friend,
                    streaks,
                    dry_run,
                    send_ledger,
                    report,
                    record,
                )
            except _AccountStopped:
                return
            except _FriendSettled:
                settled_ledger = send_ledger()
                self._add_completed(account, settled_ledger, report, [friend])
                self._add_held(account, settled_ledger, report, [friend])
                self._progress()
                continue
            except Exception as exc:  # one friend's page trouble must not hide the others
                log.exception("[%s] %s: unexpected error", account.name, friend)
                result = Receipt(Outcome.FAILED, f"异常: {brief_exc(exc)}")
                broken += 1
                paced = True
            else:
                broken = 0
                paced = result is not None and not isinstance(result, _OpenFailure)
            if result is None:  # dry-run hit
                report.sent.append(f"{friend}(dry-run)")
                report.results.append(
                    {"friend": friend, "outcome": "found", "reason": "dry-run：已找到会话，未发送"}
                )
                self._progress()
                continue
            if result.outcome is Outcome.UNCONFIRMED:
                self._snapshot(session, "send-unconfirmed")
            self._record(account, ledger, report, friend, result, dry_run)
            if not dry_run:
                # Typing or waiting for the receipt can cross midnight. Retain the
                # result/hold on both days so the next trigger cannot send it again.
                recorded_path = ledger.path
                finished_ledger = send_ledger()
                if finished_ledger.path != recorded_path:
                    finished_ledger.record(account.name, friend, result, self.now())
            self._progress()
            if result.outcome.stops_account:
                verdict = (
                    Verdict.NEEDS_LOGIN if result.outcome is Outcome.LOGGED_OUT else Verdict.BLOCKED
                )
                note = f"{friend}: {result.reason}，已停止该账号"
                self.health.pause(
                    account.name,
                    note,
                    "logged_out" if result.outcome is Outcome.LOGGED_OUT else "blocked",
                )
                self._fail(report, verdict, note)
                self._snapshot(session, result.outcome.value)
                return
            if broken >= MAX_CONSECUTIVE_ERRORS:
                note = "连续出错，浏览器可能已崩溃，已停止该账号"
                self.health.pause(account.name, note, "network_error")
                self._fail(report, Verdict.ERROR, note)
                self._snapshot(session, "error")
                return
        if report.failed and report.verdict is Verdict.DONE:
            report.verdict = Verdict.PARTIAL

    @staticmethod
    def _add_completed(
        account: Account, ledger: Ledger, report: AccountReport, targets: list[str]
    ) -> None:
        reported = {normalize_name(item["friend"]) for item in report.results}
        for friend in targets:
            canonical = normalize_name(friend)
            if canonical in reported or not ledger.is_done(account.name, friend):
                continue
            reported.add(canonical)
            entry = ledger.entry(account.name, friend) or {}
            report.already_sent.append(friend)
            report.results.append(
                {
                    "friend": friend,
                    "outcome": "already_sent",
                    "reason": "今日已发送，本次跳过；同一对象每天只发送一次。",
                    "message_id": entry.get("message_id"),
                    "sent_at": entry.get("at"),
                    "sent_date": ledger.path.stem,
                }
            )

    @staticmethod
    def _add_held(
        account: Account, ledger: Ledger, report: AccountReport, targets: list[str]
    ) -> None:
        reported = {normalize_name(item["friend"]) for item in report.results}
        for friend, entry in ledger.held(account.name, targets).items():
            canonical = normalize_name(friend)
            if canonical in reported:
                continue
            reported.add(canonical)
            outcome = entry.get("outcome", "unconfirmed")
            if not isinstance(outcome, str):
                outcome = "unconfirmed"
            hold = (
                "未取得平台回执，已暂停重复发送"
                if outcome in {"sending", "unconfirmed"}
                else "已达到当天尝试上限，请先解决失败原因"
            )
            reason = f"{entry.get('reason') or '未取得平台发送结果'}（{hold}）"
            report.failed[friend] = reason
            report.results.append(
                {
                    "friend": friend,
                    "outcome": outcome,
                    "reason": reason,
                    "message_id": entry.get("message_id"),
                    "held": True,
                    "retry_exhausted": outcome in {"failed", "friend_error"},
                }
            )
            if report.verdict is Verdict.DONE:
                report.verdict = Verdict.PARTIAL
            if not report.note:
                report.note = "部分对象发送失败，已停止重复尝试"

    def _cached_streaks(self, account: Account) -> dict[str, int]:
        """Render counts from manual fetches; metadata never supplies recipients."""
        try:
            record = SparkRecord.load(self.data.sparks_file(account.name))
        except OSError:
            log.warning("[%s] 火花天数缓存无法读取，模板天数留空", account.name)
            return {}
        wanted = {normalize_name(name) for name in account.friends}
        return {
            normalize_name(name): days
            for name, days in (record.days if record else {}).items()
            if normalize_name(name) in wanted and days >= 0
        }

    def _allowed(self, account: Account, report: AccountReport) -> bool:
        try:
            enabled = self.is_enabled(account.name) if self.is_enabled else account.enabled
        except Exception as exc:
            self._fail(report, Verdict.ERROR, f"无法核对账号启用状态，已停止发送：{brief_exc(exc)}")
            return False
        if not enabled:
            report.note = "账号已停用，已跳过"
            return False
        health = self.health.view(account.name)
        if not health["paused"]:
            return True
        verdict = {
            "logged_out": Verdict.NEEDS_LOGIN,
            "blocked": Verdict.BLOCKED,
            "ip_mismatch": Verdict.IP_MISMATCH,
        }.get(health["status"], Verdict.ERROR)
        self._fail(report, verdict, f"账号已停发：{health['reason']}")
        return False

    def _check_live(
        self,
        account: Account,
        session: ChatSession,
        record: LoginRecord,
        report: AccountReport,
        *,
        navigate: bool = True,
    ) -> bool:
        result = check_live_environment(
            session,
            record,
            account,
            self.settings,
            health=self.health,
            probe=None if navigate else getattr(session, "current_page_state", session.page_state),
            pending_note="（未发送）",
        )
        if result.ok:
            return True
        verdict = {
            "logged_out": Verdict.NEEDS_LOGIN,
            "blocked": Verdict.BLOCKED,
            "ip_mismatch": Verdict.IP_MISMATCH,
        }.get(result.status, Verdict.ERROR)
        self._fail(report, verdict, result.reason)
        if result.snapshot_label:
            self._snapshot(session, result.snapshot_label)
        return False

    @staticmethod
    def _snapshot(session: ChatSession, label: str) -> None:
        try:
            session.snapshot(label)
        except Exception:
            log.debug("snapshot failed", exc_info=True)

    def _deliver(
        self,
        account: Account,
        session: ChatSession,
        messages: MessageFactory,
        friend: str,
        streaks: dict[str, int],
        dry_run: bool,
        ledger_for_send: Callable[[], Ledger],
        report: AccountReport,
        record: LoginRecord,
    ) -> Receipt | None:
        """Open the chat and send; ``None`` means a dry-run found the friend."""
        if not session.open_friend(friend):
            reason = getattr(session, "last_open_error", None)
            if isinstance(reason, str) and reason.strip():
                return _OpenFailure(Outcome.FRIEND_ERROR, reason)
            return NOT_FOUND
        if dry_run:
            log.info("[%s] dry-run: found %s, not sending", account.name, friend)
            return None
        text = messages.build(friend, self.now().date(), streaks.get(normalize_name(friend)))
        reserved = False

        def authorize_send() -> None:
            nonlocal reserved
            if not self._check_live(account, session, record, report, navigate=False):
                raise _AccountStopped
            if not self._allowed(account, report):
                raise _AccountStopped
            ledger = ledger_for_send()
            if friend not in ledger.pending(account.name, [friend]):
                self._add_held(account, ledger, report, [friend])
                raise _FriendSettled
            ledger.reserve(account.name, friend, self.now())
            reserved = True

        try:
            guarded = getattr(session, "send_guarded", None)
            if guarded is not None:
                return guarded(text, before_submit=authorize_send, friend=friend)
            authorize_send()  # compatible non-browser adapters submit immediately
            return session.send(text)
        except (_AccountStopped, _FriendSettled):
            raise
        except SendGuardError as exc:
            note = f"发送前会话核对失败，未按发送键：{brief_exc(exc)}"
            self._pause_safely(account.name, note)
            if reserved:
                self._add_held(account, ledger_for_send(), report, [friend])
            self._fail(report, Verdict.ERROR, note)
            raise _AccountStopped from exc
        except Exception as exc:
            if not reserved:
                raise  # typing failed before submit; no send was reserved or attempted
            note = f"发送回执未知: {brief_exc(exc)}，请核对后再处理"
            self._pause_safely(account.name, note)
            return Receipt(Outcome.UNCONFIRMED, note)

    def _record(
        self,
        account: Account,
        ledger: Ledger,
        report: AccountReport,
        friend: str,
        result: Receipt,
        dry_run: bool,
    ) -> None:
        log.info("[%s] %s -> %s (%s)", account.name, friend, result.outcome.value, result.reason)
        if result.ok:
            report.sent.append(friend)
        else:
            report.failed[friend] = result.reason
        report.results.append(
            {
                "friend": friend,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "message_id": result.message_id,
            }
        )
        if not dry_run:
            ledger.record(account.name, friend, result, self.now())

    @staticmethod
    def _fail(report: AccountReport, verdict: Verdict, note: str) -> AccountReport:
        log.warning("[%s] %s", report.account, note)
        if verdict.severity >= report.verdict.severity:
            report.verdict = verdict
        report.note = note
        return report

    def _report(self, reports: list[AccountReport], dry_run: bool) -> None:
        self._reconcile_completed(reports)
        body = "\n\n".join(r.line() for r in reports)
        worst = max(reports, key=lambda r: r.verdict.severity)
        if dry_run:
            log.info("dry-run summary:\n%s", body)
            return
        crossed_day = any(
            item.get("outcome") == "previous_day_sent"
            for report in reports
            for item in report.results
        )
        if (
            worst.verdict is Verdict.DONE
            and not any(report.sent for report in reports)
            and (any(report.already_sent for report in reports) or crossed_day)
        ):
            log.info("already completed summary:\n%s", body)
            return
        if worst.verdict is Verdict.DONE:
            self.notifier.push("发送任务已完成" if crossed_day else "今日已完成", body)
            return
        retry_titles = (Verdict.BLOCKED, Verdict.NEEDS_LOGIN, Verdict.IP_MISMATCH, Verdict.PARTIAL)
        titles = {verdict: VERDICT_LABEL[verdict] for verdict in retry_titles}
        self.notifier.push(titles.get(worst.verdict, "运行异常"), body)
