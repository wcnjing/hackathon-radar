"""Relevance scoring with Claude (claude-haiku-4-5), keyword fallback otherwise."""

import hashlib
import json
import logging
import os
from typing import Literal, NamedTuple

from pydantic import BaseModel, ValidationError

from hackathon_radar.config import PROJECT_ROOT
from hackathon_radar.filtering import Kind, classify_kind_with_signal, keyword_score
from hackathon_radar.models import Event

log = logging.getLogger(__name__)

# Scoring is a judgement we want to be able to reproduce and measure, not a
# creative task: sampling randomness here shows up as events crossing the
# post/skip threshold differently between runs. Hardcoded rather than exposed in
# config.toml — a knob that can reintroduce variance defeats the purpose.
#
# This reduces variance but does NOT eliminate it, and the SDK says as much:
# "even with temperature of 0.0, the results will not be fully deterministic".
# Measured over a 25-event fixture scored in fresh processes with the cache
# below disabled: only 15/25 events held a stable score (89.6% agreement), one
# event's *kind* flipped between hackathon and networking, and two events' levels
# moved. Scores drifted by a single point — exactly enough to flip a post/skip
# decision for an event sitting on a threshold.
#
# top_k=1 (greedy decoding) was tried over 20 runs and made no reliable
# difference, so it is deliberately not set: the residual is server-side
# floating-point on shared hardware, not sampling, and no API parameter reaches
# it. Do not re-add it expecting a fix.
SCORING_TEMPERATURE = 0.0

# One keyword hit is worth 1.5 in `keyword_score`; a confident kind
# classification is worth the same. See `_fallback_score` for why.
KIND_SIGNAL_BONUS = 1.5

# ...so determinism is bought back at our own layer instead. Every batch request
# is content-addressed — schema version, model, temperature, system prompt and
# event payload hashed together — and its scores are stored. Score the same
# batch again and we return the stored answer rather than re-asking, which makes
# `score_events` a pure function of its input even though the model underneath
# is not.
#
# Any real change busts the key automatically: edit the interest profile, change
# the model, or feed different events, and the hash differs, so a stale answer
# can never mask a change you meant to make.
#
# Be honest about where this pays off. In production `_collect` only ever scores
# events the store has not seen, so an identical batch essentially never recurs
# and the hit rate is near zero; the cache buys almost nothing on the ingest
# path. Its value is elsewhere: the calibration harness (#10) re-scores one
# fixed set over and over, and a crash mid-run does not re-bill the batches that
# already succeeded.
#
# Set RADAR_NO_SCORE_CACHE=1 to bypass it and see what the model says today.
CACHE_PATH = PROJECT_ROOT / "data" / "score_cache.json"
CACHE_LIMIT = 500  # entries; oldest evicted first

# Bumped whenever `ScoredEvent` changes shape. Rows are stored as plain dicts,
# so adding, renaming or retyping a field leaves every existing row unparseable.
# Folding this into the key means such a change busts the whole cache by
# construction rather than relying on anyone remembering to clear `data/`.
CACHE_SCHEMA_VERSION = "1"


def _cache_enabled() -> bool:
    return os.environ.get("RADAR_NO_SCORE_CACHE", "").lower() not in ("1", "true")


def _cache_key(model: str, system: str, payload: str) -> str:
    digest = hashlib.sha256()
    for part in (CACHE_SCHEMA_VERSION, model, repr(SCORING_TEMPERATURE), system, payload):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")  # length-independent separator
    return digest.hexdigest()


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    # PEP 758: unparenthesized `except` tuples are valid on 3.14, and ruff's UP
    # ruleset rewrites the parenthesized form. This is `except (OSError, ValueError)`.
    except OSError, ValueError:
        return {}  # missing or corrupt: behave as a cold cache, never crash


def _cache_read(cache: dict, key: str) -> list[ScoredEvent] | None:
    """Deserialize one cached batch, or None if it is absent or unusable.

    A row that no longer matches `ScoredEvent` must read as a miss, not as an
    exception. Left to propagate it would escape `_claude_scores` into the
    blanket handler in `score_events` and log "Claude scoring failed" — sending
    the whole run to the keyword scorer over a deserialization bug, and lying
    about the cause in the one log line anyone would read.
    """
    rows = cache.get(key)
    if rows is None:
        return None
    try:
        return [ScoredEvent(**row) for row in rows]
    except (TypeError, ValidationError) as exc:
        log.warning("discarding unreadable score-cache entry (%s); re-scoring", exc)
        return None


def _cache_store(cache: dict, key: str, scored: list[dict]) -> None:
    """Add one batch and rewrite the cache file atomically."""
    cache[key] = scored
    if len(cache) > CACHE_LIMIT:
        for stale in list(cache)[: len(cache) - CACHE_LIMIT]:
            del cache[stale]
    try:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        # Write-then-rename: `write_text` truncates first, so a crash or a
        # concurrent reader mid-write would see a half-file. os.replace is
        # atomic on the same filesystem, so readers see either the old cache or
        # the new one and never a partial one.
        tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        log.warning("could not write score cache: %s", exc)


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
    kind: Kind
    level: Literal["beginner", "intermediate", "advanced", "unclear"]


class ScoreBatch(BaseModel):
    scores: list[ScoredEvent]


class ScoreResult(NamedTuple):
    """One event's score, plus whether it came from a degraded run.

    `degraded` is the difference between "the keyword scorer is how this
    deployment works" and "Claude was supposed to answer and didn't". Only the
    second is a reason to leave the event unrecorded so a later run can
    re-score it; see `cli._select`.

    A NamedTuple so `scores[key][0]` and `[1]` keep working for every existing
    reader, while the flag is available by name to the one caller that needs it.
    """

    score: float
    reason: str
    degraded: bool = False


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


def _fallback_score(event: Event, interests: dict, degraded: bool = False) -> ScoreResult:
    """Score one event without Claude: classify its kind, then keyword-score it.

    `keyword_score` alone never sets `kind`, so every fallback-scored event used
    to inherit the `Event.kind` default of "hackathon" and be judged against the
    base `min_score` — letting mixers and meetups through the very gate that
    `min_score_by_kind = { networking = 8 }` exists to close.

    A raised per-kind threshold means "only exceptional events of this kind get
    posted", and exceptional is a judgement counting keywords cannot make. So
    when the fallback puts an event in a kind whose bar is raised, its score is
    held below that bar: it cannot post on keyword hits alone.

    Note what this does *not* decide. Whether a held-down event is recorded as
    seen — and so never reconsidered — is `cli._select`'s call, and it turns on
    `degraded`. Issue #4's fix option 3 ("record and let a re-score pass handle
    them") assumed a re-score pass that did not exist; this is that pass, in the
    only form the pipeline needs: on a degraded run the event simply is not
    recorded, so the next ingest treats it as new and asks Claude again.
    """
    event.kind, matched = classify_kind_with_signal(event)
    score, reason = keyword_score(event, interests)
    min_score = interests.get("min_score", 6)

    # A confident build-event classification is itself a keyword hit, and is
    # counted as one. The keyword list and the classifier regexes are two
    # hand-maintained vocabularies for the same idea, and they had drifted:
    # "NUS Datathon 2026", "MHacks 2026" and "Hack&Roll 2026" all classify as
    # hackathons on a token the regexes know and the keyword list does not, so
    # they scored 5.0 — "none matched" — and were dropped as irrelevant.
    #
    # Crediting the classification closes the drift at its source instead of
    # asking someone to keep two lists in sync forever. It is deliberately worth
    # exactly one hit, and deliberately never applies to `networking`: a
    # match there is a reason for more suspicion, not less. `interests.keywords`
    # is left alone, being the founder's to tune.
    if matched and event.kind in ("hackathon", "program"):
        score = min(10.0, score + KIND_SIGNAL_BONUS)
        reason = f"{reason} (+{event.kind} title)"

    raised = interests.get("min_score_by_kind", {}).get(event.kind)
    if raised is not None and raised > min_score and score >= raised:
        score = float(raised) - 1.0
        reason = f"{reason} (held below the {event.kind} bar: no Claude scoring)"
    return ScoreResult(score, reason, degraded)


def _fallback_scores(
    events: list[Event], interests: dict, degraded: bool = False
) -> dict[tuple[str, str], ScoreResult]:
    return {e.key: _fallback_score(e, interests, degraded) for e in events}


def score_events(
    events: list[Event], config: dict, client=None
) -> dict[tuple[str, str], ScoreResult]:
    """Return {event.key: ScoreResult(score, reason, degraded)} for every event."""
    interests = config.get("interests", {})
    if not events:
        return {}
    if client is None:
        # Not degraded: running without Claude is a supported deployment
        # (CONTRIBUTING calls ANTHROPIC_API_KEY optional), so these scores are
        # this deployment's real answer and are recorded like any other. Marking
        # them degraded would mean a keyword-only install never records anything
        # and re-scores its whole backlog every half hour, forever.
        return _fallback_scores(events, interests)

    import anthropic

    try:
        return _claude_scores(client, events, config)
    except anthropic.AuthenticationError as exc:
        # Credentials were offered and rejected: transient or fixable, either
        # way not this deployment's intended mode. Degraded.
        log.warning("Anthropic auth failed (%s); using keyword scorer (degraded)", exc.message)
        return _fallback_scores(events, interests, degraded=True)
    except Exception:
        log.exception("Claude scoring call failed, using keyword scorer (degraded)")
        return _fallback_scores(events, interests, degraded=True)


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


def _claude_scores(client, events: list[Event], config: dict) -> dict[tuple[str, str], ScoreResult]:
    scoring_cfg = config.get("scoring", {})
    interests = config.get("interests", {})
    model = scoring_cfg.get("model", "claude-haiku-4-5")
    batch_size = scoring_cfg.get("batch_size", 20)
    system = SYSTEM_PROMPT.format(profile=interests.get("profile", "").strip())
    by_id = {f"{e.source}:{e.external_id}": e for e in events}
    results: dict[tuple[str, str], ScoreResult] = {}
    # Read the cache once per call rather than twice per batch: at the 500-entry
    # limit that was re-parsing a multi-megabyte file for every 20 events.
    cache = _cache_load() if _cache_enabled() else {}

    # Determinism has two halves, and temperature is only one of them.
    #
    # Events are scored in batches of `batch_size`, and every event in a batch
    # shares one prompt — so an event's score depends on the other events it is
    # scored alongside, not just on itself. Batch membership used to follow
    # fetch order, which varies with which sources answered and what was already
    # seen; the same event could land in a different batch on the next run and
    # score differently even at temperature 0. Sorting by the event key first
    # makes batch composition a pure function of *which* events are being
    # scored, independent of the order they arrived in.
    #
    # So: same set of events in -> same batches -> same prompts -> plus
    # temperature 0, the same scores. Change `batch_size` and scores may move,
    # because every prompt is recomposed. Anything comparing scores across runs
    # (see the calibration work in #10) must therefore hold batch_size fixed.
    ordered = sorted(events, key=lambda e: e.key)

    for i in range(0, len(ordered), batch_size):
        batch = ordered[i : i + batch_size]
        payload = json.dumps([_event_payload(e) for e in batch], ensure_ascii=False)

        key = _cache_key(model, system, payload)
        scored_batch = _cache_read(cache, key) if _cache_enabled() else None
        if scored_batch is not None:
            log.info("scoring cache hit (%d event(s))", len(scored_batch))
        else:
            response = client.messages.parse(
                model=model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": f"Score these events:\n{payload}"}],
                output_format=ScoreBatch,
                temperature=SCORING_TEMPERATURE,
            )
            scored_batch = response.parsed_output.scores
            if len(scored_batch) != len(batch):
                # A short answer is a bad answer, and caching it would freeze it
                # as this batch's permanent response: the missing events would
                # drop to the keyword scorer on every future identical run, and
                # #10's calibration harness would treat the gap as ground truth.
                # Use it for this run, but do not enshrine it.
                log.warning(
                    "model returned %d score(s) for a batch of %d; not caching this batch",
                    len(scored_batch),
                    len(batch),
                )
            elif _cache_enabled():
                _cache_store(cache, key, [s.model_dump() for s in scored_batch])

        for scored in scored_batch:
            event = by_id.get(scored.id)
            if event is not None:
                results[event.key] = ScoreResult(float(scored.score), scored.reason.strip())
                event.kind = scored.kind
                event.level = None if scored.level == "unclear" else scored.level

    # Anything the model skipped falls back to the keyword scorer. Degraded:
    # Claude answered for this run but not for this event, so the keyword score
    # is a stand-in and the event deserves another look next time.
    missing = [e for e in events if e.key not in results]
    if missing:
        log.warning("model returned no score for %d event(s); keyword-scoring them", len(missing))
    for event in missing:
        results[event.key] = _fallback_score(event, interests, degraded=True)
    return results
