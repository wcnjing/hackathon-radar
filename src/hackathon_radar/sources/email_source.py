"""Email feed — a dedicated inbox subscribed to event newsletters.

Connects over IMAP (read-only), processes each message exactly once by
tracking the last-seen UID, and has Claude extract any events from the body.
Credentials come from EMAIL_ADDRESS / EMAIL_APP_PASSWORD; the password is a
Gmail app password scoped to this one inbox.
"""

import email
import email.policy
import imaplib
import json
import logging
import os
from datetime import date

from hackathon_radar.config import PROJECT_ROOT
from hackathon_radar.enrich import _page_text
from hackathon_radar.filtering import normalize_title
from hackathon_radar.models import Event
from hackathon_radar.sources.watchlist import PageEvent, PageEvents

log = logging.getLogger(__name__)

STATE_PATH = PROJECT_ROOT / "data" / "email_state.json"

PROMPT = """Today is {today}. Below is an email sent to an inbox that is subscribed
to tech-event newsletters (subject: {subject!r}, from: {sender!r}).

Extract every tech event (hackathon, competition, workshop, meetup, talk, program)
the email announces that is upcoming or currently open for registration. Skip past
events, job/internship listings that aren't dated events, and marketing filler.
If the email announces no events, return an empty list.

For each event: title; url (the event's own link from the email, null if none);
dates_text (dates as written, null if none); location (null if not stated);
country_code (two-letter code when clear from the text, null otherwise);
is_online (true for virtual events)."""


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def body_text(msg: email.message.EmailMessage, limit: int = 6_000) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            return " ".join(plain.get_content().split())[:limit]
        except Exception:  # undecodable part; fall through to html
            pass
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        try:
            return _page_text(html_part.get_content(), limit)
        except Exception:
            pass
    return ""


def new_uids(uids: list[int], last_seen: int) -> list[int]:
    """IMAP 'UID n:*' always returns at least the newest message, so filter."""
    return sorted(u for u in uids if u > last_seen)


def to_event(pe: PageEvent, message_id: str, assume_country: str) -> Event | None:
    title = pe.title.strip()
    if not title or not pe.url:
        # An event card without a link isn't actionable; skip rather than
        # point subscribers at nothing.
        if title:
            log.info("email event %r has no link; skipped", title)
        return None
    return Event(
        source="email",
        external_id=f"{message_id}#{normalize_title(title)}",
        title=title,
        url=pe.url,
        dates_text=pe.dates_text or "",
        location=pe.location or "",
        online=pe.is_online,
        country=pe.country_code or assume_country or None,
    )


def _extract(client, model: str, text: str, subject: str, sender: str) -> list[PageEvent]:
    response = client.messages.parse(
        model=model,
        max_tokens=2_000,
        messages=[
            {
                "role": "user",
                "content": PROMPT.format(
                    today=date.today().isoformat(), subject=subject, sender=sender
                )
                + f"\n\n<email_body>\n{text}\n</email_body>",
            }
        ],
        output_format=PageEvents,
    )
    return response.parsed_output.events


def fetch(source_cfg: dict) -> list[Event]:
    address = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        log.info("EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set; skipping email source")
        return []

    from hackathon_radar.scoring import make_client

    try:
        client = make_client()
    except Exception as exc:
        log.info("email source needs Anthropic credentials (%s); skipping", exc)
        return []

    host = source_cfg.get("imap_host", "imap.gmail.com")
    folder = source_cfg.get("folder", "INBOX")
    max_emails = source_cfg.get("max_emails_per_run", 20)
    model = source_cfg.get("model", "claude-haiku-4-5")
    assume_country = source_cfg.get("assume_country", "SG")

    state = _load_state()
    last_seen = int(state.get("last_uid", 0))

    try:
        mail = imaplib.IMAP4_SSL(host)
        mail.login(address, password)
        mail.select(folder, readonly=True)
        _, data = mail.uid("search", None, f"UID {last_seen + 1}:*")
    except imaplib.IMAP4.error as exc:
        log.warning("email source IMAP failure: %s", exc)
        return []

    uids = new_uids([int(u) for u in data[0].split()], last_seen)
    if not uids:
        mail.logout()
        return []
    if len(uids) > max_emails:
        log.info("email: %d new messages, processing oldest %d", len(uids), max_emails)
        uids = uids[:max_emails]

    events: list[Event] = []
    processed_through = last_seen
    for uid in uids:
        try:
            _, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
            subject = str(msg.get("Subject", ""))[:200]
            sender = str(msg.get("From", ""))[:200]
            message_id = str(msg.get("Message-ID", f"uid-{uid}"))
            text = body_text(msg)
            if text:
                page_events = _extract(client, model, text, subject, sender)
                for pe in page_events:
                    event = to_event(pe, message_id, assume_country)
                    if event:
                        events.append(event)
                if page_events:
                    log.info("email %r: %d event(s) extracted", subject, len(page_events))
        except Exception as exc:
            log.warning("email uid %s failed (%s); will not retry", uid, exc)
        processed_through = uid

    mail.logout()
    state["last_uid"] = processed_through
    _save_state(state)
    return events
