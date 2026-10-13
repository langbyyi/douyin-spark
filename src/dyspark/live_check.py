"""The one environment gate behind ``spark check``, MCP check and the send runner.

Egress IP, page state and account identity are judged here and nowhere else, so a
given session records the same health status whatever entry point ran it. Health
is recorded (and safety statuses therefore pause the account) at the first failed
gate; callers only map the outcome onto their own reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dyspark.account_profile import OwnProfile
from dyspark.avatars import validate_avatar_url
from dyspark.chat import PageState, page_state_health
from dyspark.egress import EgressError, ip_matches
from dyspark.store import DataDir, proxy_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable

    from dyspark.account_health import HealthStore
    from dyspark.config import Account, Settings
    from dyspark.store import LoginRecord


@dataclass(frozen=True)
class LiveCheck:
    """Outcome of the shared gate; the health record is already written."""

    ok: bool
    status: str  # ready | logged_out | blocked | network_error | ip_mismatch
    reason: str
    fields: dict[str, Any] = field(default_factory=dict)
    current_ip: str | None = None
    state: PageState | None = None
    profile: OwnProfile | None = None
    snapshot_label: str | None = None


def check_live_environment(
    session: Any,
    record: LoginRecord,
    account: Account,
    settings: Settings,
    *,
    health: HealthStore,
    probe: Callable[[], PageState] | None = None,
    pending_note: str = "",
) -> LiveCheck:
    """Run the gate. ``pending_note`` (e.g. "（未发送）") marks the send runner's
    reasons, where a failed gate also means the message was not sent."""
    fields: dict[str, Any] = {
        "login_ip": record.egress_ip,
        "proxy_fingerprint": proxy_fingerprint(account.proxy),
    }
    try:
        current = session.egress_ip()
    except EgressError as exc:
        note = f"{exc}{pending_note}"
        health.record(account.name, "network_error", note, **fields)
        return LiveCheck(False, "network_error", note, fields)
    fields["current_ip"] = current
    if not ip_matches(record.egress_ip, current, settings.ip_policy):
        note = f"出口IP {current} 与登录时 {record.egress_ip} 不一致{pending_note}，请重新扫码"
        health.record(account.name, "ip_mismatch", note, **fields)
        return LiveCheck(False, "ip_mismatch", note, fields, current_ip=current)
    state = (probe or session.page_state)()
    if state is not PageState.READY:
        status, note = page_state_health(state)
        health.record(account.name, status, note, **fields)
        return LiveCheck(
            False,
            status,
            note,
            fields,
            current_ip=current,
            state=state,
            snapshot_label=state.value,
        )
    profile = getattr(session, "own_profile", lambda: None)()
    if not isinstance(profile, OwnProfile) or not profile.user_id:
        note = f"暂时无法核实抖音账号身份{pending_note}，请稍后重新检测"
        health.record(account.name, "network_error", note, **fields)
        return LiveCheck(False, "network_error", note, fields, current_ip=current, state=state)
    identity_note = "登录记录未绑定用户标识，身份未核对"
    if record.user_id:
        if isinstance(profile, OwnProfile):
            if profile.user_id != record.user_id:
                note = f"当前抖音用户与登录记录不同{pending_note}，请重新扫码确认账号"
                health.record(account.name, "blocked", note, **fields)
                return LiveCheck(
                    False,
                    "blocked",
                    note,
                    fields,
                    current_ip=current,
                    state=state,
                    profile=profile,
                    snapshot_label="identity-mismatch",
                )
            identity_note = "抖音用户标识一致"
    else:
        identity_note = "已核实抖音用户标识并绑定"
    if isinstance(profile, OwnProfile):
        refreshed = replace(
            record,
            nickname=profile.nickname,
            user_id=profile.user_id,
            avatar_url=validate_avatar_url(profile.avatar_url),
            profile_updated_at=datetime.now(settings.zone).isoformat(timespec="seconds"),
        )
        try:
            refreshed.save(DataDir(settings.data_dir).login_file(account.name))
        except OSError:
            note = f"本人资料无法保存{pending_note}，请检查账号数据目录"
            health.record(account.name, "network_error", note, **fields)
            return LiveCheck(
                False,
                "network_error",
                note,
                fields,
                current_ip=current,
                state=state,
                profile=profile,
            )
        record.nickname = refreshed.nickname
        record.user_id = refreshed.user_id
        record.avatar_url = refreshed.avatar_url
        record.profile_updated_at = refreshed.profile_updated_at
    note = f"页面就绪，出口IP检查通过；{identity_note}"
    health.record(account.name, "ready", note, **fields)
    return LiveCheck(
        True,
        "ready",
        note,
        fields,
        current_ip=current,
        state=state,
        profile=profile if isinstance(profile, OwnProfile) else None,
    )
