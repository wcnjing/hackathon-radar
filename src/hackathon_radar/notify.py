"""Telegram channel notifications via the Bot API."""

import html
import os

import httpx

from hackathon_radar.models import Event

API_BASE = "https://api.telegram.org/bot{token}"


class TelegramError(RuntimeError):
    """Telegram API failure. Never includes the request URL — it embeds the bot token."""


KIND_EMOJI = {"hackathon": "🛠", "networking": "🤝", "program": "🚀"}


def is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """True when `hour` falls in the [start, end) window; handles midnight wrap.
    start == end disables quiet hours entirely."""
    if start == end:
        return False
    if start > end:  # e.g. 23 → 8 wraps past midnight
        return hour >= start or hour < end
    return start <= hour < end


def format_message(event: Event) -> str:
    e = html.escape
    emoji = KIND_EMOJI.get(event.kind, "🛠")
    lines = [f"{emoji} <b>{e(event.title)}</b>", ""]

    meta = []
    if event.dates_text:
        meta.append(f"📅 {e(event.dates_text)}")
    if event.time_left:
        meta.append(f"⏳ {e(event.time_left)}")
    location = event.location or ("Online" if event.online else "")
    if location:
        meta.append(f"📍 {e(location)}")
    if meta:
        lines.append(" · ".join(meta))

    if event.deadline:
        lines.append(f"⏰ {e(event.deadline)}")
    if event.organizer:
        lines.append(f"👤 {e(event.organizer)}")
    if event.prize:
        lines.append(f"🏆 {e(event.prize)} in prizes")
    if event.team_size:
        lines.append(f"👥 {e(event.team_size)}")
    if event.invite_only:
        lines.append("🔒 Invite only")
    if event.tags:
        lines.append(f"🏷 {e(', '.join(event.tags[:4]))}")
    # Scores and Claude's relevance reasons stay in the database — the public
    # channel gets factual event info only.
    if event.brief:
        # Collapsed by default in Telegram; tap to expand.
        lines.append(f"<blockquote expandable>{e(event.brief)}</blockquote>")
    lines.append(f'🔗 <a href="{e(event.url)}">{e(event.url)}</a>')
    if event.register_url:
        lines.append(f'📝 <a href="{e(event.register_url)}">Register here</a>')
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, silent: bool = False) -> None:
        self._call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            disable_notification=silent,
        )

    def get_updates(self) -> list[dict]:
        return self._call("getUpdates")

    def _call(self, method: str, **payload):
        resp = httpx.post(
            API_BASE.format(token=self.token) + f"/{method}", json=payload, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not data.get("ok"):
            description = data.get("description") or f"HTTP {resp.status_code}"
            raise TelegramError(f"Telegram {method} failed: {description}")
        return data["result"]
