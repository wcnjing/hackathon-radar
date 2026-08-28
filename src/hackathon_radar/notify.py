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

# There is deliberately no "is this event team-based?" check here.
#
# One was tried and removed after three review rounds and fifteen wrong
# answers. team_size is free text written by Claude during enrichment, and it
# has no grammar to parse against:
#
#   "solo or teams up to 5"                  team allowed
#   "No teams permitted"                     solo only
#   "No team required"                       team allowed — opposite meaning
#   "individual members only"                solo only, despite "member"
#   "Teams of 1 (2026 edition)"              solo only; a year looks like a size
#   "Teams of 1, 48-hour sprint"             solo only; a duration looks like one
#   "1 member per team, $5,000 prize pool"   solo only; a prize looks like one
#
# Every fix for one row broke another. The guard also only ever fired on
# Devpost, because enrichment populates team_size for no other source (see
# enrich.py), and most hackathons allow teams regardless — an elaborate
# mechanism for a rare case, wrong in both directions.
#
# The costs are asymmetric. A stray prompt on a solo event is untidy, and the
# linked page states the real rules. A suppressed prompt on a "no team
# required" event hides it from exactly the students it exists for. So the
# prompt goes on every hackathon.


def build_reply_markup(event: Event, label: str) -> dict | None:
    """A single URL button under the card, or None to send without a keyboard.

    The button points at `event.url`, the informative detail page, because
    `register_url` is never populated by any source — Devpost deliberately
    leaves it unset so cards land on the overview rather than a signup wall.
    The label lives in config so that wording can be revisited without a
    deploy.
    """
    if not label or not event.url:
        return None
    return {"inline_keyboard": [[{"text": label, "url": event.url}]]}


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
        if event.kind == "hackathon":
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

    def send(self, text: str, silent: bool = False, reply_markup: dict | None = None) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "disable_notification": silent,
        }
        # Omitted rather than sent as null: Telegram treats an explicit null as
        # an instruction to strip the keyboard.
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._call("sendMessage", **payload)

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
