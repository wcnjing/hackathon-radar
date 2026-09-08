"""Enrich Devpost events with team size and a challenge brief.

Team size and the actual challenge live in free-form page text (the rules page
buries team size in legalese that varies per hackathon), so we fetch the pages
and let Claude extract structured fields. Runs only for new events; without
Anthropic credentials it's skipped and the fields stay None.
"""

import html as html_lib
import logging
import re

import httpx
from pydantic import BaseModel

from hackathon_radar.models import Event

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

PROMPT = """From the hackathon page text below, extract:

- team_size: the allowed team size as a short phrase, e.g. "solo or teams up to 5",
  "teams of 2-4", "solo only". Use null if the pages don't clearly say.
- brief: 2-3 plain sentences on what participants actually build or submit (the
  challenge / problem statement) and any notable constraint or required tech.
  No marketing fluff. Use null if the pages don't make it clear.
- deadline: the registration or submission deadline as a short phrase with the
  date, e.g. "register by Aug 17, 2026" or "submissions due Jul 10, 5pm ET".
  Use null if no explicit deadline is stated."""


class EventDetails(BaseModel):
    team_size: str | None
    brief: str | None
    deadline: str | None


_ANCHOR_RE = re.compile(r"<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def _keep_link(match: re.Match) -> str:
    """Render `<a href=U>text</a>` as `text (U)` so extraction can attach real
    URLs to events — stripping tags outright destroyed every link."""
    href, inner = match.group(1), match.group(2)
    if href.startswith(("http://", "https://")) and len(href) <= 300:
        return f"{inner} ({href})"
    return inner


def _page_text(html: str, limit: int) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = _ANCHOR_RE.sub(_keep_link, text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def _eligibility_excerpt(rules_html: str) -> str:
    text = _page_text(rules_html, 80_000)
    match = re.search(r"[Ee]ligib", text)
    return text[match.start() : match.start() + 2_500] if match else text[:2_000]


def enrich_events(events: list[Event], config: dict, client) -> None:
    """Mutates Devpost events in place; other sources are left untouched."""
    targets = [e for e in events if e.source == "devpost"]
    if not targets or client is None:
        return

    import anthropic

    model = config.get("scoring", {}).get("model", "claude-haiku-4-5")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as web:
        for event in targets:
            try:
                _enrich_one(event, web, client, model)
            except anthropic.AuthenticationError:
                log.warning("Anthropic auth failed; skipping enrichment for this run")
                return
            except Exception as exc:
                log.warning("enrichment failed for %s: %s", event.title, exc)


def _enrich_one(event: Event, web: httpx.Client, client, model: str) -> None:
    landing = _page_text(web.get(event.url).text, 4_000)
    rules = ""
    try:
        rules = _eligibility_excerpt(web.get(event.url.rstrip("/") + "/rules").text)
    except httpx.HTTPError:
        pass

    response = client.messages.parse(
        model=model,
        max_tokens=1_000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{PROMPT}\n\n<landing_page>\n{landing}\n</landing_page>\n\n"
                    f"<rules_excerpt>\n{rules}\n</rules_excerpt>"
                ),
            }
        ],
        output_format=EventDetails,
    )
    details = response.parsed_output
    event.team_size = details.team_size
    event.brief = details.brief
    event.deadline = details.deadline
