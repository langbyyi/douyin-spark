"""Project log wording and credential masking, shared by writers and historical views."""

from __future__ import annotations

import re

# Match complete project messages only. Unknown/third-party diagnostics stay intact.
_RULES = [
    (
        r"(\[[^\]\n]+\] )?cloud run without proxy: datacenter IPs are often risk-controlled",
        r"\1项目提醒：云端未配置账号代理，当前使用直连",
    ),
    (
        r"logged in from a datacenter IP without proxy; sends may be risk-controlled",
        "项目提醒：云端账号未配置代理，当前使用直连",
    ),
    (
        r"dyspark (\S+) (\S+) \(data: (.*), run: (.*)\)",
        r"dyspark \1 · 执行 \2（数据目录：\3；运行记录：\4）",
    ),
    (r"finished in ([\d.]+)s \(exit (-?\d+)\)", r"执行结束，耗时 \1 秒，退出码 \2"),
    (r"egress IP via (.*): (\S+)", r"出口 IP：\2（查询服务：\1）"),
    (r"trimmed ([\d.]+) MB of browser cache", r"已清理浏览器缓存 \1 MB"),
    (r"(\[[^\]\n]+\] )?(\d+) target\(s\), (\d+) pending", r"\1共 \2 位对象，待发送 \3 位"),
    (r"no send response captured: (.*)", r"未捕获发送回执：\1"),
    (
        r"notify: (.*) suppressed \(same title pushed recently\)",
        r"已跳过重复通知：\1（同类通知近期已推送）",
    ),
    (r"notify disabled, dropping: (.*)", r"推送已关闭，跳过通知：\1"),
    (r"notify: (.*) \| (.*)", r"推送通知：\1 | \2"),
    (r"service log attached: (.*) \(dyspark (.*)\)", r"服务日志已启用：\1（dyspark \2）"),
    (r"(\[[^\]\n]+\] )?run crashed", r"\1执行异常"),
    (r"unexpected error", "执行发生异常"),
    (r"GUI request failed: (.*)", r"面板请求失败：\1"),
    (r"gui error", "面板请求发生异常"),
    (r"scheduler tick failed", "自动计划检查失败"),
    (r"scheduler: could not launch run: (.*)", r"自动计划启动任务失败：\1"),
    (r"page is busy during login wait: (.*)", r"登录页面仍在加载：\1"),
    (r"page is busy during login: (.*)", r"登录页面仍在加载：\1"),
    (r"chat page is still busy: (.*)", r"聊天页面仍在加载：\1"),
    (r"QR code expired, refreshing", "二维码已过期，正在刷新"),
    (r"browser close failed: (.*)", r"关闭浏览器失败：\1"),
    (r"screenshot failed: (.*)", r"保存截图失败：\1"),
    (r"late send evidence unavailable: (.*)", r"无法读取后续发送回执：\1"),
    (r"send response identity unavailable: (.*)", r"无法核对发送回执标识：\1"),
    (r"save raw send response failed: (.*)", r"保存原始发送回执失败：\1"),
    (r"send observer cleanup failed: (.*)", r"清理发送监听器失败：\1"),
    (r"(server酱|pushplus) push failed: (.*)", r"\1 推送请求失败：\2"),
    (r"(server酱|pushplus) returned a non-JSON answer", r"\1 推送返回了非 JSON 响应"),
    (r"(server酱|pushplus) rejected the push: (.*)", r"\1 未接受推送：\2"),
    (r"notify state read failed: (.*)", r"读取推送状态失败：\1"),
    (r"notify state write failed: (.*)", r"保存推送状态失败：\1"),
    (r"(.*) not in conversation list, trying search", r"会话列表未找到 \1，正在搜索"),
    (r"closing the browser failed: (.*)", r"关闭浏览器失败：\1"),
    (r"page busy while probing state: (.*)", r"检查页面状态时仍在加载：\1"),
    (r"conversation diagnostics unavailable: (.*)", r"无法读取会话诊断信息：\1"),
    (r"late send receipt unavailable: (.*)", r"无法读取后续发送回执：\1"),
    (r"could not save the raw send response: (.*)", r"保存原始发送回执失败：\1"),
    (r"egress echo (.*) failed: (.*)", r"出口 IP 查询失败（\1）：\2"),
    (r"egress IP \(IPv6 only\): (.*)", r"出口 IP（仅 IPv6）：\1"),
    (r"closing the echo tab failed: (.*)", r"关闭 IP 查询页面失败：\1"),
    (r"could not write (.*): (.*)", r"写入 \1 失败：\2"),
    (r"cleanup failed: (.*)", r"清理历史数据失败：\1"),
    (r"random start delay ([\d.]+)s", r"随机等待 \1 秒后开始"),
    (
        r"(\[[^\]\n]+\] )could not persist pause; account is stopped for this run",
        r"\1无法保存停发状态，本次已停止该账号",
    ),
    (r"(\[[^\]\n]+\] )(.*): unexpected error", r"\1\2：执行发生异常"),
    (r"(\[[^\]\n]+\] )dry-run: found (.*), not sending", r"\1预演：已找到 \2"),
    (r"scheduler unavailable: (.*)", r"自动计划不可用：\1"),
    (r"scheduler tick failed: (.*)", r"自动计划检查失败：\1"),
    (r"hitokoto unavailable, using fallback: (.*)", r"在线一言不可用，已使用回退文案：\1"),
    (r"cannot chmod (.*)", r"无法设置目录权限：\1"),
    (r"(.*) is corrupt, moved to (.*)", r"\1 已损坏，已备份至 \2"),
    (r"removing stale run marker: (.*)", r"清理失效的运行标记：\1"),
    (r"temporary QR endpoint listening on :(\d+)", r"临时扫码服务已启动，端口 \1"),
    (r"login page busy: (.*)", r"登录页面仍在加载：\1"),
    (r"login relay listening on (.*):(\d+)", r"扫码中转服务已启动：\1:\2"),
    (r"login page: SMS code requested(.*)", r"已请求短信验证码\1"),
    (r"login command (.*) failed", r"登录操作 \1 失败"),
    (r"login action: (.*)", r"登录操作：\1"),
    (r"noVNC fallback unavailable: (.*)", r"远程浏览器不可用：\1"),
    (r"dyspark GUI (.*) → (.*)  \(config=(.*)\)", r"dyspark \1 面板已启动：\2（配置：\3）"),
    (r"dyspark (.*) MCP server \(config: (.*)\)", r"dyspark \1 MCP 服务已启动（配置：\2）"),
    (
        r"cannot serve the QR link on port (\d+) \((.*)\); file/push only",
        r"扫码链接服务无法使用端口 \1：\2；已保留文件与推送方式",
    ),
]
_EXACT = {
    "self-profile response unavailable": "无法读取账号资料响应",
    "self-profile wait unavailable": "等待账号资料失败",
    "submission observer unavailable during cleanup": "清理发送监听器时无法访问页面",
    "send identity unavailable": "无法读取发送标识",
    "check snapshot failed": "保存检查截图失败",
    "check failed": "账号检查失败",
    "friends failed": "拉取会话失败",
    "progress callback failed": "更新任务进度失败",
    "nothing left to send today": "今日没有待发送对象",
    "snapshot failed": "保存页面截图失败",
    "dry-run summary:": "预演结果：",
    "already completed summary:": "今日已完成：",
    "cannot read ledger; sending is blocked": "无法读取发送账本，已停止发送",
    "cannot persist ledger inspection hold; sending remains blocked": (
        "无法保存账本保护状态，发送仍已停止"
    ),
    "historical ledger is unreadable; sending is blocked": "无法读取历史发送账本，已停止发送",
    "relay listener failed": "扫码中转监听失败",
    "login page: SMS verification requested": "已提交短信验证码",
    "login command qr_refresh: official QR refreshed": "已刷新抖音二维码",
    "login command sms_send: platform control clicked": "已点击抖音获取验证码按钮",
    "login command sms_verify: platform control clicked": "已点击抖音提交验证码按钮",
    "login.public_url is not set: the link may not be reachable from a phone": (
        "未配置扫码访问地址，手机可能无法打开链接"
    ),
}
_COMPILED_RULES = [(re.compile(pattern), replacement) for pattern, replacement in _RULES]
_LOG_LINE = re.compile(
    r"^((?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+"
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+)([^:\s]+)(:\s?)(.*)$"
)
_URL_AUTH = re.compile(r"(\b(?:https?|socks5h?)://)[^\s/@]+@", re.IGNORECASE)
_URL_SECRET = re.compile(
    r"([?&](?:token|access_token|sendkey|password|key|secret)=)[^\s&#\"']+", re.IGNORECASE
)
_SENDKEY = re.compile(
    r"(https?://(?:sctapi\.ftqq\.com/|\d+\.push\.ft07\.com/send/))[^\s/]+(\.send)",
    re.IGNORECASE,
)
_QUOTED_SECRET = re.compile(
    r"([\"'](?:token|access_token|sendkey|password|secret|cookie|authorization)[\"']"
    r"\s*:\s*)([\"'])((?:\\.|(?!\2)[^\\])*)(\2)",
    re.IGNORECASE,
)


def mask_log_secrets(text: str) -> str:
    text = _URL_AUTH.sub(r"\1***@", text)
    text = _URL_SECRET.sub(r"\1***", text)
    text = _SENDKEY.sub(r"\1***\2", text)
    return _QUOTED_SECRET.sub(r"\1\2***\4", text)


def localize_message(logger: str, message: str) -> str:
    if logger != "dyspark" and not logger.startswith("dyspark."):
        return message
    first, separator, rest = message.partition("\n")
    if first in _EXACT:
        return _EXACT[first] + separator + rest
    for pattern, replacement in _COMPILED_RULES:
        if pattern.fullmatch(first):
            return pattern.sub(replacement, first) + separator + rest
    return message


def log_views(lines: list[str]) -> dict[str, list[str]]:
    """Raw text is retained except credentials; readable text translates known messages."""
    raw = [mask_log_secrets(line) for line in lines]
    readable = []
    for line in raw:
        match = _LOG_LINE.fullmatch(line)
        if match:
            prefix, logger, separator, message = match.groups()
            line = prefix + logger + separator + localize_message(logger, message)
        readable.append(line)
    return {"lines": raw, "display_lines": readable}
