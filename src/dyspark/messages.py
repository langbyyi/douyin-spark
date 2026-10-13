"""Message text rendering: random template + optional hitokoto quote + placeholders.

Placeholders: ``{date}`` ``{weekday}`` ``{friend}`` ``{days}`` ``{daily_quote}``
``{hitokoto}`` ``{source}``.
Anything else in braces (unknown names, stray ``{`` or ``}``) is kept literally, so a
template can never make a run fail.
"""

from __future__ import annotations

import json
import logging
import random
import re
import urllib.request
from dataclasses import dataclass
from datetime import date

from dyspark.config import MessageOptions

log = logging.getLogger(__name__)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
EMPTY_FALLBACK = "🔥"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
DAILY_QUOTES = (
    "把日子过成自己喜欢的样子，慢一点也没关系。",
    "今天的小小进步，也值得为自己鼓掌。",
    "愿你忙有所值，闲有所趣。",
    "不用赶上谁，走好自己的路就很好。",
    "给生活一点耐心，美好正在路上。",
    "认真生活的人，总能遇见细碎的光。",
    "偶尔停下来，也是在照顾向前走的自己。",
    "别忘了，你也值得被温柔对待。",
    "把开心攒起来，留给需要勇气的日子。",
    "今天先做好一件小事，就已经很棒了。",
    "有些答案，会在好好生活的途中出现。",
    "愿你的每一份努力，都慢慢长出回响。",
    "允许自己休息，明天仍然可以重新出发。",
    "生活不必每一天都精彩，平安顺心也很好。",
    "带着好奇心出门，普通的一天也有惊喜。",
    "别把自己绷得太紧，笑一笑再继续。",
    "珍惜眼前的小确幸，就是幸福的开始。",
    "你走过的每一步，都在积攒新的可能。",
    "愿今天的烦恼，止于今天的晚风。",
    "努力之外，也记得给自己一点偏爱。",
    "把期待放在明天，把认真留给今天。",
    "不必事事满分，尽心就值得肯定。",
    "照顾好自己，是每天最重要的小事。",
    "愿你心里有光，脚下有自己的方向。",
    "一顿好饭，一个好觉，也是生活的奖赏。",
    "平凡的坚持，会让日子一点点变好。",
    "遇到喜欢的小事，就大方地开心一下。",
    "今天的你，也可以成为自己的依靠。",
    "不急着证明什么，踏实前行就好。",
    "愿我们都能在忙碌里，留一点时间给快乐。",
)


def message_presets(today: date) -> list[dict[str, str | list[str]]]:
    """Return editable account-level templates, without changing saved configuration."""
    presets = [
        {
            "id": "daily-quote",
            "name": "每日一言",
            "description": "30 句 · 每日轮换 · 离线可用",
            "templates": [
                "每日一言 ✨\n{daily_quote}",
                "{friend}，分享今天的一句话：\n{daily_quote}",
                "{date} {weekday} · 今日份小美好\n{daily_quote}",
            ],
        },
        {
            "id": "warm-greeting",
            "name": "暖心问候",
            "description": "日常关怀与祝福",
            "templates": [
                "{friend}，今天也要好好吃饭，照顾好自己呀 🍀",
                "送你一份今日份的好心情，愿忙碌之余也有小开心 ☀️",
                "{weekday}好呀，愿你今天遇到的都是温柔的小事 ✨",
            ],
        },
        {
            "id": "casual-checkin",
            "name": "轻松打卡",
            "description": "日常问候与打卡",
            "templates": [
                "叮，今日份打卡已送达，火花继续营业 🔥",
                "路过来打个招呼，今天也要开开心心～",
                "{friend}，今日份冒泡 🫧 希望你今天一切顺利！",
            ],
        },
    ]
    for preset in presets:
        preset["example"] = render(
            preset["templates"][0], today=today, friend="朋友", days=None, quote=Quote("")
        )
    return presets


@dataclass(frozen=True, slots=True)
class Quote:
    text: str
    source: str = ""


def fetch_quote(options: MessageOptions, timeout: float = 6.0) -> Quote:
    """Fetch a hitokoto quote; fall back to the configured text on any error."""
    request = urllib.request.Request(options.hitokoto_url, headers={"User-Agent": "dyspark"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected hitokoto payload")
        text = str(payload.get("hitokoto") or "").strip()
        if text:
            return Quote(text, str(payload.get("from") or "").strip())
    except Exception as exc:
        log.warning("hitokoto unavailable, using fallback: %s", exc)
    return Quote(options.hitokoto_fallback)


def render(template: str, *, today: date, friend: str, days: int | None, quote: Quote) -> str:
    """Fill the known placeholders; never raises. An empty result becomes 🔥."""
    values = {
        "date": f"{today.month:02d}月{today.day:02d}日",  # no strftime: locale-safe on Windows
        "weekday": WEEKDAYS[today.weekday()],
        "friend": friend,
        "days": "" if days is None else str(days),
        "daily_quote": DAILY_QUOTES[today.toordinal() % len(DAILY_QUOTES)],
        "hitokoto": quote.text,
        "source": quote.source,
    }
    text = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    return text.strip() or EMPTY_FALLBACK


class MessageFactory:
    """Produces one message per friend; fetches a fresh quote only when a template needs it."""

    def __init__(
        self, options: MessageOptions, templates: tuple[str, ...] = (), rng=random
    ) -> None:
        self.options = options
        self.templates = templates or options.templates
        self.rng = rng

    def build(self, friend: str, today: date, days: int | None = None) -> str:
        template = self.rng.choice(self.templates)
        needs_quote = "{hitokoto}" in template or "{source}" in template
        quote = (
            fetch_quote(self.options)
            if needs_quote and self.options.hitokoto
            else Quote(self.options.hitokoto_fallback)
        )
        return render(template, today=today, friend=friend, days=days, quote=quote)
