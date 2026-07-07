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
    location = event.location or ("Online" if event.online else "")
    if location:
        meta.append(f"📍 {e(location)}")
    if meta:
        lines.append(" · ".join(meta))

    if event.prize:
        lines.append(f"🏆 {e(event.prize)} in prizes")
    if event.tags:
        lines.append(f"🏷 {e(', '.join(event.tags[:4]))}")
    lines.append(f"💡 {e(reason)} ({score:.0f}/10)")
    lines.append(f'🔗 <a href="{e(event.url)}">{e(event.url)}</a>')
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
