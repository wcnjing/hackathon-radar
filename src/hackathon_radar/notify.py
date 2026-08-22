"""Telegram channel notifications via the Bot API."""

import html
import os

import httpx

from hackathon_radar.models import Event

API_BASE = "https://api.telegram.org/bot{token}"


class TelegramError(RuntimeError):
    """Telegram API failure. Never includes the request URL — it embeds the bot token."""


KIND_EMOJI = {"hackathon": "🛠", "networking": "🤝", "program": "🚀"}

# Shown only when the event's own text signals a level; no badge beats a wrong badge.
LEVEL_LINE = {
    "beginner": "🌱 Beginner friendly",
    "intermediate": "⚡ Some experience helpful",
    "advanced": "🔥 Advanced / competitive",
}


# Gate 1 experiment. Only shown once a discussion group is linked to the
# channel — without one there is nowhere to reply, and a card that points
# nowhere burns trust.
#
# Deliberately low-cost and neutral: the public step is just raising a hand.
# Year, skills and goals are collected privately in a DM follow-up, because a
# public "state your experience" selects against the beginners matching is for.
TEAM_PROMPT = "🙋 <b>Looking for teammates?</b> Reply below."
COMPANY_PROMPT = "👋 <b>Anyone else going?</b> Reply below."

# A hackathon is team-based unless its own text says otherwise. "solo or teams
# up to 5" must read as team-based, so team wording wins over solo wording.
_TEAM_HINTS = ("team", "member", "group")
_SOLO_HINTS = ("individual", "solo only", "no teams", "one person", "single participant")


def is_team_based(event: Event) -> bool:
    """Whether teammates make sense for this event.

    team_size is only populated by enrichment (Devpost today), so most events
    arrive unknown. Unknown defaults to True: hackathons are team events by
    convention, and the cost of a missing prompt is higher than a stray one.
    """
    text = (event.team_size or "").lower()
    if any(h in text for h in _TEAM_HINTS):
        return True
    if any(h in text for h in _SOLO_HINTS):
        return False
    return True


def is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """True when `hour` falls in the [start, end) window; handles midnight wrap.
    start == end disables quiet hours entirely."""
    if start == end:
        return False
    if start > end:  # e.g. 23 → 8 wraps past midnight
        return hour >= start or hour < end
    return start <= hour < end


def format_message(event: Event, team_prompt: bool = False) -> str:
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

    if event.level in LEVEL_LINE:
        lines.append(LEVEL_LINE[event.level])
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
    if team_prompt:
        if event.kind == "hackathon" and is_team_based(event):
            lines.extend(["", TEAM_PROMPT])
        elif event.kind == "networking":
            lines.extend(["", COMPANY_PROMPT])
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
