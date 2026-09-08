"""Email feed — a dedicated inbox subscribed to event newsletters.

Connects over IMAP (read-only), processes each message exactly once by
tracking the last-seen UID, and has Claude extract any events from the body.
Credentials come from EMAIL_ADDRESS / EMAIL_APP_PASSWORD; the password is a
Gmail app password scoped to this one inbox.
"""

import base64
import email
import email.policy
import imaplib
import io
import json
import logging
import os
import re
from datetime import date

from hackathon_radar.config import PROJECT_ROOT
from hackathon_radar.enrich import _page_text
from hackathon_radar.filtering import normalize_title
from hackathon_radar.models import Event
from hackathon_radar.sources.watchlist import PageEvent, PageEvents

log = logging.getLogger(__name__)

STATE_PATH = PROJECT_ROOT / "data" / "email_state.json"

PROMPT = """Today is {today}. Below is an email sent to an inbox that is subscribed
to tech-event newsletters (subject: {subject!r}, from: {sender!r}). It may include
attachment content: calendar-invite fields, extracted PDF text, or attached poster
images shown above this message — read those too.

Extract every tech event (hackathon, competition, workshop, meetup, talk, program)
the email or its attachments announce that is upcoming or currently open for
registration. Links appear inline as "text (url)" — attach the event's own url.
Prefer the most informative public event page where a newcomer can read details
and decide. Avoid direct signup/login walls, Google Forms, "apply now" pages,
calendar-add links, and tracking redirects when a public detail page is available.
Skip past events, job/internship listings that aren't dated events, and marketing
filler. If the email announces no events, return an empty list.

For each event: title; url (the best public detail link from the email, null if none);
dates_text (dates as written, null if none); location (null if not stated);
country_code (two-letter code when clear from the text, null otherwise);
is_online (true for virtual events)."""


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except OSError, ValueError:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def body_text(msg: email.message.EmailMessage, limit: int = 6_000) -> str:
    plain = ""
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        try:
            plain = " ".join(plain_part.get_content().split())[:limit]
        except Exception:
            pass
    html = ""
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        try:
            html = _page_text(html_part.get_content(), limit)
        except Exception:
            pass
    # Prefer the plain part, but not when it's empty or link-free while the
    # HTML part carries the URLs (common with marketing senders) — link-free
    # text produces events the pipeline must drop as non-actionable.
    if plain and ("http" in plain or not html):
        return plain
    return html or plain


MAX_ATTACHMENT_BYTES = 3_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGES = 2
MAX_DOCS = 2


def _ics_text(raw: str, limit: int = 1_500) -> str:
    """Flatten the first VEVENT's key fields — structured gold, no AI needed."""
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    fields = []
    for key in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "URL", "DESCRIPTION"):
        match = re.search(rf"^{key}[^:]*:(.+)$", unfolded, re.M)
        if match:
            fields.append(f"{key.title()}: {match.group(1).strip()}")
    return (f"Calendar invite — {'; '.join(fields)}")[:limit] if fields else ""


def _pdf_text(data: bytes, limit: int = 2_000) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:3])
        return " ".join(text.split())[:limit]
    except Exception:
        return ""


def attachment_content(msg: email.message.EmailMessage) -> tuple[str, list[dict]]:
    """Returns (extra_text, image_blocks) from an email's attachments:
    .ics and PDF become text; poster images become Claude vision blocks."""
    texts: list[str] = []
    images: list[dict] = []
    for part in msg.iter_attachments():
        ctype = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            continue
        if ctype == "text/calendar" and isinstance(payload, str):
            if ics := _ics_text(payload):
                texts.append(ics)
        elif not isinstance(payload, bytes) or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        elif ctype == "application/pdf" and len(texts) < MAX_DOCS:
            if pdf := _pdf_text(payload):
                name = part.get_filename() or "attachment.pdf"
                texts.append(f"PDF attachment {name!r}: {pdf}")
        elif ctype in IMAGE_TYPES and len(images) < MAX_IMAGES:
            images.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": ctype,
                        "data": base64.b64encode(payload).decode(),
                    },
                }
            )
    return "\n".join(texts), images


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


def _extract(
    client,
    model: str,
    text: str,
    subject: str,
    sender: str,
    images: list[dict] | None = None,
) -> list[PageEvent]:
    prompt = (
        PROMPT.format(today=date.today().isoformat(), subject=subject, sender=sender)
        + f"\n\n<email_body>\n{text}\n</email_body>"
    )
    response = client.messages.parse(
        model=model,
        max_tokens=2_000,
        messages=[{"role": "user", "content": [*(images or []), {"type": "text", "text": prompt}]}],
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
        status, select_data = mail.select(folder, readonly=True)
        if status != "OK":
            log.warning("email: select %r failed: %s %s", folder, status, select_data)
            return []
        total = int(select_data[0])
        _, data = mail.uid("search", None, f"UID {last_seen + 1}:*")
    except imaplib.IMAP4.error as exc:
        log.warning("email source IMAP failure: %s", exc)
        return []

    uids = new_uids([int(u) for u in data[0].split()], last_seen)
    log.info(
        "email: %s has %d message(s); %d new since uid %d",
        folder,
        total,
        len(uids),
        last_seen,
    )
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
            extra_text, images = attachment_content(msg)
            text = "\n".join(part for part in (body_text(msg), extra_text) if part)
            if text or images:
                page_events = _extract(client, model, text, subject, sender, images)
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
