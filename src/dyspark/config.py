"""Configuration: ``config.toml`` for behaviour, environment / ``.env`` for secrets.

Validation errors are raised as :class:`SettingsError` with a Chinese message that
names the offending key, so a typo never surfaces as a traceback.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_RESERVED_ACCOUNT_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
IP_POLICIES = ("exact", "subnet")
CHANNELS = ("chrome", "chromium", "msedge")
PROXY_SCHEMES = ("http", "https", "socks4", "socks5")

# IPv4-only services first (dual-stack ones may answer with IPv6), China-reachable first.
DEFAULT_IP_ECHO_URLS = (
    "https://myip.ipip.net/",
    "https://ip.3322.net/",
    "https://ddns.oray.com/checkip",
    "https://api.ipify.org/?format=json",
    "https://ifconfig.me/ip",
)

DEFAULT_HITOKOTO_URL = "https://v1.hitokoto.cn/?encode=json"
DEFAULT_HITOKOTO_FALLBACK = "今天也要元气满满"
DEFAULT_MESSAGE_TEMPLATES = (
    "今天也来续个火花 🔥",
    "来打个卡，火花别断啦～",
    "火花续上，今天也要开心呀 ✨",
)

_SECTION_KEYS: dict[str, frozenset[str]] = {
    "general": frozenset(
        {"timezone", "keep_days", "ip_policy", "save_raw_responses", "ip_echo_urls"}
    ),
    "schedule": frozenset({"times", "enabled"}),
    "timing": frozenset(
        {"jitter_minutes", "gap_seconds", "typing_delay_ms", "send_timeout_seconds"}
    ),
    "browser": frozenset({"channel", "executable_path", "offscreen", "headless"}),
    "messages": frozenset({"templates", "hitokoto", "hitokoto_url", "hitokoto_fallback"}),
    # on_success 已废弃（每次执行完毕都推送）：保留键名只为容忍旧配置文件，解析时忽略其值
    "notify": frozenset({"enabled", "on_success"}),
    "login": frozenset({"public_url", "bind", "port", "timeout_minutes", "headless"}),
    "mcp": frozenset({"enabled"}),
}
_ACCOUNT_KEYS = frozenset({"name", "friends", "proxy", "templates", "enabled", "remark"})
_IGNORED_ACCOUNT_KEYS = frozenset(
    {"auto_spark"}
)  # Read old files without restoring retired fields.


class SettingsError(ValueError):
    """The configuration file or environment is invalid."""


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    friends: tuple[str, ...]
    proxy: str | None = None
    templates: tuple[str, ...] = ()
    enabled: bool = True
    remark: str = ""


@dataclass(frozen=True, slots=True)
class Timing:
    jitter_minutes: float = 10
    gap_seconds: tuple[float, float] = (25, 80)
    typing_delay_ms: tuple[int, int] = (60, 170)
    send_timeout_seconds: float = 15


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    channel: str = "chrome"
    executable_path: str | None = None
    offscreen: bool = True
    headless: bool = False  # 无头运行 run/check/friends；更隐蔽，但更容易触发抖音风控


@dataclass(frozen=True, slots=True)
class MessageOptions:
    templates: tuple[str, ...] = DEFAULT_MESSAGE_TEMPLATES
    hitokoto: bool = False
    hitokoto_url: str = DEFAULT_HITOKOTO_URL
    hitokoto_fallback: str = DEFAULT_HITOKOTO_FALLBACK


@dataclass(frozen=True, slots=True)
class NotifyOptions:
    enabled: bool = True
    serverchan_key: str | None = None
    pushplus_token: str | None = None


@dataclass(frozen=True, slots=True)
class LoginOptions:
    """Where the temporary login endpoints listen (QR link, web relay, noVNC)."""

    public_url: str | None = None  # e.g. http://119.91.206.33:8789 as seen from a phone
    bind: str = "0.0.0.0"
    port: int = 8789
    timeout_minutes: float = 10
    vnc_port: int = 6080
    vnc_password: str | None = None
    headless: bool = True  # web relay only; window/qr/vnc modes remain headed

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_minutes * 60


@dataclass(frozen=True, slots=True)
class Settings:
    accounts: tuple[Account, ...]
    data_dir: Path
    timezone: str = "Asia/Shanghai"
    keep_days: int = 7
    schedule_times: tuple[str, ...] = ("12:30", "20:30")
    ip_policy: str = "exact"
    ip_echo_urls: tuple[str, ...] = DEFAULT_IP_ECHO_URLS
    save_raw_responses: bool = True
    timing: Timing = field(default_factory=Timing)
    browser: BrowserOptions = field(default_factory=BrowserOptions)
    messages: MessageOptions = field(default_factory=MessageOptions)
    notify: NotifyOptions = field(default_factory=NotifyOptions)
    cloud: bool = False
    login: LoginOptions = field(default_factory=LoginOptions)
    schedule_enabled: bool = False
    mcp_enabled: bool = True

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def account(self, name: str) -> Account:
        for account in self.accounts:
            if account.name == name:
                return account
        known = "、".join(a.name for a in self.accounts)
        raise SettingsError(f"未知账号 {name!r}（已配置：{known}）")


# ----------------------------------------------------------------------------- helpers
def validate_account_name(name: str) -> str:
    """Validate a portable profile directory identifier, preserving its case."""
    if not _ACCOUNT_NAME.fullmatch(name):
        raise SettingsError(f"账号名 {name!r} 只能包含字母、数字、_ 和 -（1~32 个字符）")
    if name.upper() in _RESERVED_ACCOUNT_NAMES:
        raise SettingsError(f"账号名 {name!r} 是系统保留名称，请换一个名称")
    return name


def expand_env(value: str, env: Mapping[str, str], key: str = "") -> str:
    """Replace ``${NAME}`` references; an undefined variable is a configuration error."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if not env.get(name):
            raise SettingsError(f"{key} 引用的环境变量 {name} 未设置（请在 .env 中定义）")
        return env[name]

    return _ENV_REF.sub(substitute, value)


def _table(doc: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise SettingsError(f"[{name}] 必须是一个表（section）")
    unknown = sorted(set(value) - _SECTION_KEYS[name])
    if unknown:
        raise SettingsError(f"[{name}] 中有未知配置项：{', '.join(unknown)}")
    return value


def _bool(raw: Any, key: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise SettingsError(f"{key} 必须是 true 或 false（不要加引号）")
    return raw


def _number(raw: Any, key: str, default: float, *, minimum: float = 0) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise SettingsError(f"{key} 必须是数字")
    if isinstance(raw, float) and not math.isfinite(raw):
        raise SettingsError(f"{key} 必须是有限数字")
    if raw < minimum:
        raise SettingsError(f"{key} 不能小于 {minimum:g}")
    return raw


def _pair(raw: Any, key: str, default: tuple[float, float]) -> tuple[float, float]:
    if raw is None:
        return default
    if not (isinstance(raw, list) and len(raw) == 2):
        raise SettingsError(f"{key} 必须是两个数字的列表，例如 [最小值, 最大值]")
    low, high = (_number(v, key, 0) for v in raw)
    if high < low:
        raise SettingsError(f"{key} 需要满足 0 <= 最小值 <= 最大值")
    return low, high


def _strings(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise SettingsError(f'{key} 必须是字符串列表，例如 ["a", "b"]')
    return tuple(x.strip() for x in raw if x.strip())


def _text(raw: Any, key: str, default: str) -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise SettingsError(f"{key} 必须是字符串")
    return raw.strip()


def _http_url(value: str, key: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - malformed bracketed hosts/ports are configuration errors
    except ValueError as exc:
        raise SettingsError(f"{key} 网址格式无效") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc or not host:
        raise SettingsError(f"{key} 必须是 http(s):// 开头的网址，当前为 {value!r}")
    return value


def validate_proxy(proxy: str, key: str) -> str:
    """Check ``scheme://[user:pass@]host[:port]`` and the browser's limitations."""
    try:
        parts = urlsplit(proxy)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a malformed host or port
    except ValueError as exc:
        raise SettingsError(f"{key} 的主机或端口无效") from exc
    if parts.scheme.lower() not in PROXY_SCHEMES or not host:
        raise SettingsError(
            f"{key} 格式应为 http://[用户名:密码@]主机:端口 或 socks5://主机:端口，"
            f"支持的协议：{', '.join(PROXY_SCHEMES)}"
        )
    if parts.scheme.lower().startswith("socks") and parts.username:
        raise SettingsError(
            f"{key}：Chrome 不支持带用户名密码的 SOCKS 代理。请改用 http:// 代理，"
            "或使用免认证（IP 白名单）的 SOCKS 代理"
        )
    if parts.path not in ("", "/") or parts.query:
        raise SettingsError(f"{key} 只能包含协议、认证信息、主机和端口")
    return proxy


def proxy_credentials(proxy: str) -> tuple[str, str]:
    """Decoded ``(username, password)`` of a proxy URL (percent-escapes resolved)."""
    parts = urlsplit(proxy)
    return unquote(parts.username or ""), unquote(parts.password or "")


# ----------------------------------------------------------------------------- parsing
def validate_account_remark(raw: Any) -> str:
    if (
        not isinstance(raw, str)
        or len(raw) > 64
        or any(unicodedata.category(ch) == "Cc" for ch in raw)
    ):
        raise SettingsError("账号备注须为 64 字以内的文字，不能包含换行或控制字符")
    return raw.strip()


def _parse_account(raw: Any, env: Mapping[str, str]) -> Account:
    if not isinstance(raw, dict):
        raise SettingsError("[[accounts]] 的每一项都必须是表")
    name = validate_account_name(str(raw.get("name", "")).strip())
    unknown = sorted(set(raw) - _ACCOUNT_KEYS - _IGNORED_ACCOUNT_KEYS)
    if unknown:
        raise SettingsError(f"账号 {name} 中有未知配置项：{', '.join(unknown)}")
    friends = _strings(raw.get("friends"), f"accounts.{name}.friends")
    duplicates = sorted({f for f in friends if friends.count(f) > 1})
    if duplicates:
        raise SettingsError(f"accounts.{name}.friends 中有重复的好友：{'、'.join(duplicates)}")
    enabled = _bool(raw.get("enabled"), f"accounts.{name}.enabled", True)
    proxy_key = f"accounts.{name}.proxy"
    proxy = expand_env(_text(raw.get("proxy"), proxy_key, ""), env, proxy_key).strip() or None
    if proxy:
        validate_proxy(proxy, proxy_key)
    templates = _strings(raw.get("templates"), f"accounts.{name}.templates")
    remark = validate_account_remark(raw.get("remark", ""))
    return Account(
        name=name,
        friends=friends,
        proxy=proxy,
        templates=templates,
        enabled=enabled,
        remark=remark,
    )


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = (env.get(name) or "").strip()
    if not value:
        return default
    if not value.isdigit() or not 0 < int(value) < 65536:
        raise SettingsError(f"环境变量 {name} 必须是 1~65535 之间的端口号")
    return int(value)


def _env_text(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def _port(raw: Any, key: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 < raw < 65536:
        raise SettingsError(f"{key} 必须是 1~65535 之间的整数端口")
    return raw


def _bind_address(value: str, key: str) -> str:
    if value == "localhost":
        return value
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise SettingsError(
            f"{key} 必须是 IP 地址（如 0.0.0.0 或 127.0.0.1），当前为 {value!r}"
        ) from exc
    return value


def _parse_login(doc: Mapping[str, Any], env: Mapping[str, str]) -> LoginOptions:
    """``[login]`` plus the environment overrides used by Docker (env wins)."""
    public_url = _env_text(env, "SPARK_PUBLIC_URL") or _text(
        doc.get("public_url"), "login.public_url", ""
    )
    if public_url:
        _http_url(public_url, "login.public_url / SPARK_PUBLIC_URL")
    bind = _env_text(env, "SPARK_LOGIN_BIND") or _text(doc.get("bind"), "login.bind", "0.0.0.0")
    port = _port(doc.get("port"), "login.port", 8789)
    timeout = _number(doc.get("timeout_minutes"), "login.timeout_minutes", 10, minimum=1)
    if timeout > 60:
        raise SettingsError("login.timeout_minutes 不能超过 60")
    return LoginOptions(
        public_url=public_url.rstrip("/") or None,
        bind=_bind_address(bind, "login.bind / SPARK_LOGIN_BIND"),
        port=_env_int(env, "SPARK_LOGIN_HTTP_PORT", port),
        timeout_minutes=timeout,
        vnc_port=_env_int(env, "SPARK_VNC_PORT", 6080),
        vnc_password=_env_text(env, "SPARK_VNC_PASSWORD"),
        # 解析期收敛成 bool 的唯一决策点：默认无头；设置 SPARK_VNC_PASSWORD 意味着
        # 云端人工验证场景，默认改为有头，出现滑块/图形验证时可经 noVNC 处理。
        # 显式 login.headless 始终优先（详见 docs/configuration.md [login] 一节）。
        headless=_bool(
            doc.get("headless"), "login.headless", not bool(_env_text(env, "SPARK_VNC_PASSWORD"))
        ),
    )


def parse_settings(
    doc: Mapping[str, Any],
    data_dir: Path,
    env: Mapping[str, str] | None = None,
    *,
    config_dir: Path | None = None,
) -> Settings:
    """Build :class:`Settings` from a parsed TOML document plus the environment."""
    env = os.environ if env is None else env
    unknown = sorted(set(doc) - set(_SECTION_KEYS) - {"accounts"})
    if unknown:
        raise SettingsError(f"未知的配置段：{', '.join(unknown)}")
    general, schedule = _table(doc, "general"), _table(doc, "schedule")
    timing_doc, browser_doc = _table(doc, "timing"), _table(doc, "browser")
    message_doc, notify_doc = _table(doc, "messages"), _table(doc, "notify")
    mcp = _table(doc, "mcp")
    login = _parse_login(_table(doc, "login"), env)

    raw_accounts = doc.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise SettingsError("accounts 必须写成 [[accounts]] 表数组")
    accounts = tuple(_parse_account(a, env) for a in raw_accounts)
    names = [a.name.casefold() for a in accounts]
    if len(set(names)) != len(names):
        raise SettingsError("账号名 name 不能重复（不区分大小写）")

    timezone = _text(general.get("timezone"), "general.timezone", "Asia/Shanghai")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError(f"general.timezone 无效：{timezone!r}（例如 Asia/Shanghai）") from exc
    ip_policy = _text(general.get("ip_policy"), "general.ip_policy", "exact")
    if ip_policy not in IP_POLICIES:
        raise SettingsError(f"general.ip_policy 只能是 {' 或 '.join(IP_POLICIES)}")
    echo_urls = tuple(
        _http_url(u, "general.ip_echo_urls")
        for u in _strings(general.get("ip_echo_urls"), "general.ip_echo_urls")
    )
    channel = _text(browser_doc.get("channel"), "browser.channel", "chrome")
    if channel not in CHANNELS:
        raise SettingsError(f"browser.channel 只能是 {'、'.join(CHANNELS)}")
    executable = _text(browser_doc.get("executable_path"), "browser.executable_path", "") or None
    if executable:
        try:
            executable_file = Path(executable).expanduser()
            if config_dir is not None and not executable_file.is_absolute():
                executable_file = config_dir / executable_file
            executable_file = executable_file.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise SettingsError("browser.executable_path 路径无效") from exc
        if not executable_file.is_file():
            raise SettingsError(f"browser.executable_path 指向的文件不存在：{executable_file}")
        executable = str(executable_file)

    times = _strings(schedule.get("times", ["12:30", "20:30"]), "schedule.times")
    bad = [t for t in times if not _TIME.match(t)]
    if bad or not times:
        raise SettingsError(f'schedule.times 必须是 HH:MM 格式（如 "12:30"），有误：{bad}')
    templates = _strings(
        message_doc.get("templates", list(DEFAULT_MESSAGE_TEMPLATES)), "messages.templates"
    )
    if not templates:
        raise SettingsError("messages.templates 不能为空")
    hitokoto_url = _http_url(
        _text(message_doc.get("hitokoto_url"), "messages.hitokoto_url", DEFAULT_HITOKOTO_URL),
        "messages.hitokoto_url",
    )
    return Settings(
        accounts=accounts,
        data_dir=data_dir,
        timezone=timezone,
        keep_days=int(_number(general.get("keep_days"), "general.keep_days", 7, minimum=1)),
        schedule_times=times,
        ip_policy=ip_policy,
        ip_echo_urls=echo_urls or DEFAULT_IP_ECHO_URLS,
        save_raw_responses=_bool(
            general.get("save_raw_responses"), "general.save_raw_responses", True
        ),
        timing=Timing(
            jitter_minutes=_number(timing_doc.get("jitter_minutes"), "timing.jitter_minutes", 10),
            gap_seconds=_pair(timing_doc.get("gap_seconds"), "timing.gap_seconds", (25, 80)),
            typing_delay_ms=tuple(  # type: ignore[arg-type]
                int(v)
                for v in _pair(
                    timing_doc.get("typing_delay_ms"), "timing.typing_delay_ms", (60, 170)
                )
            ),
            send_timeout_seconds=_number(
                timing_doc.get("send_timeout_seconds"), "timing.send_timeout_seconds", 15, minimum=1
            ),
        ),
        browser=BrowserOptions(
            channel=channel,
            executable_path=executable,
            offscreen=_bool(browser_doc.get("offscreen"), "browser.offscreen", True),
            headless=_bool(browser_doc.get("headless"), "browser.headless", False),
        ),
        messages=MessageOptions(
            templates=templates,
            hitokoto=_bool(message_doc.get("hitokoto"), "messages.hitokoto", False),
            hitokoto_url=hitokoto_url,
            hitokoto_fallback=_text(
                message_doc.get("hitokoto_fallback"),
                "messages.hitokoto_fallback",
                DEFAULT_HITOKOTO_FALLBACK,
            )
            or DEFAULT_HITOKOTO_FALLBACK,
        ),
        notify=NotifyOptions(
            serverchan_key=_env_text(env, "SPARK_SERVERCHAN_KEY"),
            pushplus_token=_env_text(env, "SPARK_PUSHPLUS_TOKEN"),
            enabled=_bool(notify_doc.get("enabled"), "notify.enabled", True),
        ),
        cloud=(_env_text(env, "SPARK_ENV") or "").lower() == "cloud",
        login=login,
        schedule_enabled=_bool(schedule.get("enabled"), "schedule.enabled", False),
        mcp_enabled=_bool(mcp.get("enabled"), "mcp.enabled", True),
    )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        values = dotenv_values(path, encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettingsError(f"{path} 不是 UTF-8 编码，请用 UTF-8 保存") from exc
    return {k: v for k, v in values.items() if v is not None}


def load_env(config_path: Path) -> dict[str, str]:
    """The sibling ``.env`` merged under the real environment (real variables win)."""
    return {**_read_env_file(config_path.parent / ".env"), **os.environ}


def read_config_text(config_path: Path) -> str:
    if not config_path.is_file():
        raise SettingsError(f"找不到配置文件：{config_path}（可复制 config.example.toml）")
    try:
        return config_path.read_text(encoding="utf-8-sig")  # tolerate Notepad's BOM
    except UnicodeDecodeError as exc:
        raise SettingsError(f"{config_path} 不是 UTF-8 编码，请用 UTF-8 保存") from exc


def settings_from_text(text: str, config_path: Path, data_dir: Path | None = None) -> Settings:
    """Parse and validate configuration text as if it were stored at ``config_path``."""
    env = load_env(config_path)
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{config_path} 语法错误：{exc}") from exc
    root = data_dir or Path(_env_text(env, "SPARK_DATA_DIR") or config_path.parent / "data")
    return parse_settings(doc, root.expanduser().resolve(), env, config_dir=config_path.parent)


def read_settings(config_path: Path, data_dir: Path | None = None) -> Settings:
    """Read ``config_path`` plus a sibling ``.env`` (real environment variables win)."""
    return settings_from_text(read_config_text(config_path), config_path, data_dir)
