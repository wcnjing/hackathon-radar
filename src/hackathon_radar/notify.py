"""Telegram channel notifications via the Bot API."""

import html
import os

import httpx

from hackathon_radar.models import Event

API_BASE = "https://api.telegram.org/bot{token}"


def format_message(event: Event, score: float, reason: str) -> str:
    e = html.escape
    lines = [f"🛠 <b>{e(event.title)}</b>"]

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
    if reason:
        lines.append(f"💡 {e(reason)} ({score:.0f}/10)")
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

    def send(self, text: str) -> None:
        resp = httpx.post(
            API_BASE.format(token=self.token) + "/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        resp.raise_for_status()

    def get_updates(self) -> list[dict]:
        resp = httpx.get(API_BASE.format(token=self.token) + "/getUpdates", timeout=30)
        resp.raise_for_status()
        return resp.json().get("result", [])
