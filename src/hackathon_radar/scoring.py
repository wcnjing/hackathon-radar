"""Relevance scoring with Claude (claude-haiku-4-5), keyword fallback otherwise."""

import json
import logging
import os
from typing import Literal

from pydantic import BaseModel

from hackathon_radar.filtering import classify_kind, keyword_score
from hackathon_radar.models import Event

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You score hackathons and tech events for relevance to a specific audience.

The audience profile:
{profile}

For each event, return a score from 0 (irrelevant) to 10 (must-see) and a single short
sentence explaining why this audience would (or wouldn't) care. Judge from the title,
tags, location, dates, and prize. Score every event you are given, using its exact id.

Also classify each event's kind:
- "hackathon" — build-and-submit competitions, buildathons, hack sprints, game jams
- "networking" — meetups, talks, socials, demo days, mixers, founder breakfasts
- "program" — accelerators, fellowships, multi-week structured programs

And its experience level, ONLY when the event's text clearly signals it:
- "beginner" — first-timers welcome, no experience needed, intro workshops, student hackathons
- "intermediate" — assumes you can already build; typical dev meetups and open hackathons
- "advanced" — expert/practitioner crowd or seriously competitive (qualifiers, huge prizes)
- "unclear" — the text doesn't say. Prefer "unclear" over guessing: a wrong
  "beginner" label burns trust with exactly the students this serves."""


class ScoredEvent(BaseModel):
    id: str
    score: int
    reason: str
    kind: Literal["hackathon", "networking", "program"]
    level: Literal["beginner", "intermediate", "advanced", "unclear"]


class ScoreBatch(BaseModel):
    scores: list[ScoredEvent]


def _event_payload(event: Event) -> dict:
    return {
        "id": f"{event.source}:{event.external_id}",
        "title": event.title,
        "tags": event.tags,
        "location": event.location or ("Online" if event.online else "unknown"),
        "dates": event.dates_text,
        "prize": event.prize,
        "organizer": event.organizer,
        "team_size": event.team_size,
        "challenge": event.brief,
        "source": event.source,
    }


def _fallback_score(event: Event, interests: dict) -> tuple[float, str]:
    """Score one event without Claude: classify its kind, then keyword-score it.

    `keyword_score` alone never sets `kind`, so every fallback-scored event used
    to inherit the `Event.kind` default of "hackathon" and be judged against the
    base `min_score` — letting mixers and meetups through the very gate that
    `min_score_by_kind = { networking = 8 }` exists to close.

    A raised per-kind threshold means "only exceptional events of this kind get
    posted", and exceptional is a judgement counting keywords cannot make. So
    when the fallback puts an event in a kind whose bar is raised, its score is
    held below that bar: the event is still recorded (and so still deduped and
    never re-notified), it just cannot post on keyword hits alone.
    """
    event.kind = classify_kind(event)
    score, reason = keyword_score(event, interests)
    min_score = interests.get("min_score", 6)
    raised = interests.get("min_score_by_kind", {}).get(event.kind)
    if raised is not None and raised > min_score and score >= raised:
        score = float(raised) - 1.0
        reason = f"{reason} (held below the {event.kind} bar: no Claude scoring)"
    return score, reason


def _fallback_scores(events: list[Event], interests: dict):
    return {e.key: _fallback_score(e, interests) for e in events}


def score_events(
    events: list[Event], config: dict, client=None
) -> dict[tuple[str, str], tuple[float, str]]:
    """Return {event.key: (score, reason)} for every event."""
    interests = config.get("interests", {})
    if not events:
        return {}
    if client is None:
        return _fallback_scores(events, interests)

    import anthropic

    try:
        return _claude_scores(client, events, config)
    except anthropic.AuthenticationError as exc:
        log.warning("Anthropic auth failed (%s); using keyword scorer", exc.message)
        return _fallback_scores(events, interests)
    except Exception:
        log.exception("Claude scoring failed, falling back to keyword scorer")
        return _fallback_scores(events, interests)


def make_client():
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        # An explicit key must also clear auth_token: if a stale
        # ANTHROPIC_AUTH_TOKEN lingers in the environment the SDK would send
        # both credentials and the API rejects the request.
        return anthropic.Anthropic(api_key=api_key, auth_token=None)
    # Otherwise let the SDK resolve ANTHROPIC_AUTH_TOKEN or an
    # `ant auth login` profile; no credentials raises at construction.
    return anthropic.Anthropic()


def _claude_scores(
    client, events: list[Event], config: dict
) -> dict[tuple[str, str], tuple[float, str]]:
    scoring_cfg = config.get("scoring", {})
    interests = config.get("interests", {})
    model = scoring_cfg.get("model", "claude-haiku-4-5")
    batch_size = scoring_cfg.get("batch_size", 20)
    system = SYSTEM_PROMPT.format(profile=interests.get("profile", "").strip())
    by_id = {f"{e.source}:{e.external_id}": e for e in events}
    results: dict[tuple[str, str], tuple[float, str]] = {}

    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        payload = json.dumps([_event_payload(e) for e in batch], ensure_ascii=False)
        response = client.messages.parse(
            model=model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": f"Score these events:\n{payload}"}],
            output_format=ScoreBatch,
        )
        for scored in response.parsed_output.scores:
            event = by_id.get(scored.id)
            if event is not None:
                results[event.key] = (float(scored.score), scored.reason.strip())
                event.kind = scored.kind
                event.level = None if scored.level == "unclear" else scored.level

    # Anything the model skipped falls back to the keyword scorer.
    for event in events:
        if event.key not in results:
            results[event.key] = _fallback_score(event, interests)
    return results
