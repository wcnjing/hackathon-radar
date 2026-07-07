"""Relevance scoring with Claude (claude-haiku-4-5), keyword fallback otherwise."""

import json
import logging
import os

from pydantic import BaseModel

from hackathon_radar.filtering import keyword_score
from hackathon_radar.models import Event

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You score hackathons and tech events for relevance to one specific user.

The user's interest profile:
{profile}

For each event, return a score from 0 (irrelevant) to 10 (must-see) and a single short
sentence explaining why this user would (or wouldn't) care. Judge from the title, tags,
location, dates, and prize. Score every event you are given, using its exact id."""


class ScoredEvent(BaseModel):
    id: str
    score: int
    reason: str


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
        "team_size": event.team_size,
        "challenge": event.brief,
        "source": event.source,
    }


def score_events(
    events: list[Event], config: dict, client=None
) -> dict[tuple[str, str], tuple[float, str]]:
    """Return {event.key: (score, reason)} for every event."""
    interests = config.get("interests", {})
    if not events:
        return {}
    if client is None:
        return {e.key: keyword_score(e, interests) for e in events}

    import anthropic

    try:
        return _claude_scores(client, events, config)
    except anthropic.AuthenticationError as exc:
        log.warning("Anthropic auth failed (%s); using keyword scorer", exc.message)
        return {e.key: keyword_score(e, interests) for e in events}
    except Exception:
        log.exception("Claude scoring failed, falling back to keyword scorer")
        return {e.key: keyword_score(e, interests) for e in events}


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

    # Anything the model skipped falls back to the keyword scorer.
    for event in events:
        if event.key not in results:
            results[event.key] = keyword_score(event, interests)
    return results
