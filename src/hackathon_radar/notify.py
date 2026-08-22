"""Telegram channel notifications via the Bot API."""

import html
import os
import re

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

# team_size is free text written by Claude during enrichment, so this has to
# cope with real phrasings: "Teams up to 4", "solo or teams up to 5",
# "1 member", "No teams permitted", "individual members only".
#
# Order matters. An explicit negation or solo wording has to beat the word
# "team" appearing elsewhere in the sentence, and a numeric limit of 1 has to
# beat both. "member" is deliberately NOT team evidence: "individual members
# only" is solo, while "1-4 members" is settled by the number instead.
_NO_TEAMS_RE = re.compile(r"\bno\s+teams?\b")
_SOLO_RE = re.compile(r"\b(individuals?|solo|alone|single)\b")
_TEAM_RE = re.compile(r"\b(teams?|groups?)\b")
_NUM_RE = re.compile(r"\d+")


def is_team_based(event: Event) -> bool:
    """Whether teammates make sense for this event.

    Enrichment only populates team_size for Devpost today, so most events
    arrive with None. Unknown defaults to True: hackathons are team events by
    convention, a missing prompt costs Gate 1 data, and a stray one is merely
    untidy. We suppress only on positive evidence of solo-only entry.

    Known limitation: the numeric rule assumes every number describes team
    size, so "1 person, up to 3 submissions" would read as team-based.
    """
    text = (event.team_size or "").lower()
    if not text:
        return True
    if _NO_TEAMS_RE.search(text):
        return False
    # Solo wording only counts when nothing offers a team alternative, so
    # "solo or teams up to 5" falls through to the numeric rule below.
    if _SOLO_RE.search(text) and not _TEAM_RE.search(text):
        return False
    sizes = [int(n) for n in _NUM_RE.findall(text)]
    if sizes:
        return max(sizes) > 1
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
