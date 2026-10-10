"""Degraded scoring: when Claude was expected but did not answer.

A keyword guess standing in for a model score must be neither posted nor
recorded, because `store.record` is one-way: `_collect` only scores events the
store has not seen, so a recorded guess is never looked at again.
"""

import json
import tomllib
from pathlib import Path

import httpx
import pytest
from scoring_fakes import CONFIG, INTERESTS, _AuthFails, _Explodes, _ScoresEverything, make_event

from hackathon_radar import cli
from hackathon_radar.cli import _select
from hackathon_radar.models import Event
from hackathon_radar.scoring import (
    ScoreBatch,
    ScoredEvent,
    ScoreResult,
    score_events,
)
from hackathon_radar.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Degraded scores must not be recorded as final
# ---------------------------------------------------------------------------


class TestDegradedFlagging:
    """`degraded` distinguishes "Claude failed" from "this install has no key"."""

    def test_transient_api_error_marks_results_degraded(self):
        event = make_event(title="AI Student Hackathon")
        result = score_events([event], CONFIG, _Explodes())[event.key]
        assert result.degraded is True

    def test_auth_failure_marks_results_degraded(self):
        event = make_event(title="AI Student Hackathon")
        result = score_events([event], CONFIG, _AuthFails())[event.key]
        assert result.degraded is True

    def test_no_client_is_a_supported_mode_not_a_degradation(self):
        """Running without an API key is documented as supported. Marking it
        degraded would mean such an install records nothing and re-scores its
        whole backlog every run, forever."""
        event = make_event(title="AI Student Hackathon")
        result = score_events([event], CONFIG, None)[event.key]
        assert result.degraded is False

    def test_claude_scored_events_are_not_degraded(self):
        event = make_event(title="AI Student Hackathon")
        result = score_events([event], CONFIG, _ScoresEverything())[event.key]
        assert result.degraded is False

    def test_events_the_model_skipped_are_degraded(self):
        """Claude answered for the run but not for this event, so its keyword
        score is a stand-in and deserves another look."""

        class SkipsOne:
            class messages:
                @staticmethod
                def parse(**kw):
                    return type("R", (), {"parsed_output": ScoreBatch(scores=[])})()

        event = make_event(title="AI Student Hackathon")
        result = score_events([event], CONFIG, SkipsOne())[event.key]
        assert result.degraded is True

    def test_score_and_reason_still_unpack_positionally(self):
        """Existing readers index [0] and [1]; that must keep working."""
        event = make_event()
        result = score_events([event], CONFIG, None)[event.key]
        score, reason = result[0], result[1]
        assert score == result.score and reason == result.reason


class TestDegradedRunsAreRevisitable:
    """The review's Critical finding, in one sentence: a guess must not be
    recorded, because recording is one-way and nothing re-scores."""

    def _select_one(self, tmp_path, result, dry_run=False):
        event = make_event(title="Deep Learning Workshop", location="Singapore")
        store = Store(tmp_path / "t.db")
        selected = _select([event], {event.key: result}, store, CONFIG, 99, dry_run)
        seen = store.is_seen(event)
        store.close()
        return event, selected, seen

    def test_a_degraded_skip_is_left_unrecorded(self, tmp_path):
        _, selected, seen = self._select_one(tmp_path, ScoreResult(2.0, "weak", degraded=True))
        assert selected == []
        assert seen is False, "a degraded skip was recorded and can never be re-scored"

    def test_a_normal_skip_is_still_recorded(self, tmp_path):
        _, selected, seen = self._select_one(tmp_path, ScoreResult(2.0, "weak", degraded=False))
        assert selected == []
        assert seen is True

    def test_a_high_scoring_degraded_event_is_not_queued_either(self, tmp_path):
        """The mirror image, and the one that actually reaches subscribers. A
        keyword guess can be *higher* than the score it stands in for, so acting
        on the optimistic half while deferring the pessimistic half would post
        exactly what the channel exists to filter out."""
        events = [make_event(title=f"AI Hackathon {i}", external_id=str(i)) for i in range(3)]
        scores = {e.key: ScoreResult(9.0, "good", degraded=True) for e in events}
        store = Store(tmp_path / "t.db")
        selected = _select(events, scores, store, CONFIG, cap=99, dry_run=False)
        unseen = [e for e in events if not store.is_seen(e)]
        store.close()
        assert selected == [], "a guessed 9.0 was queued for posting"
        assert len(unseen) == 3, "degraded events must stay re-scorable"

    def test_the_same_score_posts_once_it_is_not_a_guess(self, tmp_path):
        """Guard the premise: 9.0 clears the bar, so the test above is measuring
        the degraded flag and not an unrelated threshold failure."""
        event = make_event(title="AI Hackathon")
        store = Store(tmp_path / "t.db")
        selected = _select([event], {event.key: ScoreResult(9.0, "good")}, store, CONFIG, 99, False)
        store.close()
        assert [e.external_id for e in selected] == ["1"]

    def test_duplicate_titles_are_recorded_even_when_degraded(self, tmp_path):
        """A duplicate title is a fact about the store, not about the score:
        re-scoring would reach the same conclusion, so record it and move on."""
        store = Store(tmp_path / "t.db")
        first = make_event(title="Repeat Hackathon", external_id="1")
        store.record(first, 9.0, "x")
        store.mark_notified(first)
        dupe = make_event(title="Repeat Hackathon", external_id="2")
        _select([dupe], {dupe.key: ScoreResult(9.0, "x", degraded=True)}, store, CONFIG, 99, False)
        seen = store.is_seen(dupe)
        store.close()
        assert seen is True

    def test_dry_run_still_records_nothing(self, tmp_path):
        _, _, seen = self._select_one(
            tmp_path, ScoreResult(2.0, "weak", degraded=False), dry_run=True
        )
        assert seen is False

    def test_a_transient_error_costs_nothing_permanently(self, tmp_path, monkeypatch):
        """End to end: an outage, then recovery. The event must survive the
        outage and post once Claude comes back."""
        event = make_event(title="Deep Learning Workshop", location="Singapore")
        store = Store(tmp_path / "t.db")

        outage = score_events([event], CONFIG, _Explodes())
        _select([event], outage, store, CONFIG, 99, dry_run=False)
        assert store.is_seen(event) is False, "buried during the outage"

        # Next run: the event is still unseen, so _collect would re-score it.
        recovered = score_events([event], CONFIG, _ScoresEverything(score=9, kind="hackathon"))
        selected = _select([event], recovered, store, CONFIG, 99, dry_run=False)
        store.close()
        assert [e.title for e in selected] == ["Deep Learning Workshop"]

    def test_degraded_run_logs_one_summary_line(self, tmp_path, caplog):
        events = [make_event(title=f"Thing {i}", external_id=str(i)) for i in range(3)]
        scores = {e.key: ScoreResult(1.0, "weak", degraded=True) for e in events}
        store = Store(tmp_path / "t.db")
        with caplog.at_level("WARNING"):
            _select(events, scores, store, CONFIG, 99, dry_run=False)
        store.close()
        summary = [r for r in caplog.records if "DEGRADED RUN" in r.message]
        assert len(summary) == 1
        assert "3" in summary[0].getMessage()

    def test_healthy_run_logs_no_degradation_warning(self, tmp_path, caplog):
        event = make_event()
        store = Store(tmp_path / "t.db")
        with caplog.at_level("WARNING"):
            _select([event], {event.key: ScoreResult(1.0, "w")}, store, CONFIG, 99, False)
        store.close()
        assert not [r for r in caplog.records if "DEGRADED" in r.message]


# ---------------------------------------------------------------------------
# Recovery through the real CLI path
# ---------------------------------------------------------------------------


class TestOutageRecoveryThroughIngest:
    """`_ingest` for real, not a dry run -- a dry run records nothing either way,
    so it could not tell the fix from the bug.

    The event here is deliberately one the keyword scorer cannot judge: no
    keyword hits and no kind signal, so it defaults to networking at 5.0 and is
    skipped. That is the case the review was about -- an event Claude would rate
    highly, thrown away by a guess.
    """

    BLIND_SPOT = "Quantum Computing Primer"

    def _config(self):
        return {
            **CONFIG,
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }

    def test_the_keyword_scorer_really_does_skip_this_one(self):
        """Guard the premise: if this event ever starts passing, the two tests
        below would silently stop testing anything."""
        event = make_event(title=self.BLIND_SPOT)
        result = score_events([event], CONFIG, None)[event.key]
        threshold = INTERESTS["min_score_by_kind"].get(event.kind, INTERESTS["min_score"])
        assert result.score < threshold

    def test_an_outage_leaves_the_store_empty_so_the_next_run_re_scores(
        self, tmp_path, monkeypatch
    ):
        event = make_event(title=self.BLIND_SPOT)
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])

        monkeypatch.setattr(cli, "make_client", lambda: _Explodes())
        cli._ingest(self._config(), store)
        assert store.queue_depth() == 0
        assert store.is_seen(event) is False, "the outage buried the event permanently"

        # Claude recovers. The event is still unseen, so _collect re-scores it.
        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        cli._ingest(self._config(), store)
        depth = store.queue_depth()
        store.close()
        assert depth == 1, "the event never came back after the outage"

    def test_a_healthy_low_score_is_recorded_and_not_retried(self, tmp_path, monkeypatch):
        """The other direction: a real judgement must still be final, or the
        pipeline would re-score the same rejects forever."""
        event = make_event(title=self.BLIND_SPOT)
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])
        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(1, "networking"))
        cli._ingest(self._config(), store)
        seen, depth = store.is_seen(event), store.queue_depth()
        store.close()
        assert seen is True
        assert depth == 0

    def test_keyword_only_deployments_still_record(self, tmp_path, monkeypatch):
        """No API key is a supported mode, not an outage. If these were left
        unrecorded the install would re-score its whole backlog every run."""
        event = make_event(title=self.BLIND_SPOT)
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])
        monkeypatch.setattr(
            cli, "make_client", lambda: (_ for _ in ()).throw(RuntimeError("no key"))
        )
        cli._ingest(self._config(), store)
        seen = store.is_seen(event)
        store.close()
        assert seen is True


# ---------------------------------------------------------------------------
# One failed batch must not discard the others
# ---------------------------------------------------------------------------


class TestBatchFailureIsContained:
    """A 529 on batch N used to escape the whole loop, so `score_events`
    keyword-scored every event -- including the ones Claude had already scored
    in batches 1..N-1. Because a keyword guess can outscore the rejection it
    replaced, those events then cleared the bar and were queued.
    """

    CONFIG = {**CONFIG, "scoring": {"batch_size": 2}}

    def _events(self, n=4):
        return [
            Event(
                source="devpost",
                external_id=f"e{i}",
                title=f"Crypto Hackathon {i}",
                url="https://example.com",
            )
            for i in range(n)
        ]

    class FailsOnBatch:
        """Scores every batch but the nth, which raises."""

        def __init__(self, fail_on=2, exc=None):
            self.fail_on, self.exc = fail_on, exc or RuntimeError("529 Overloaded")
            self.calls = 0
            self.messages = self

        def parse(self, **kw):
            self.calls += 1
            if self.calls == self.fail_on:
                raise self.exc
            ids = [e["id"] for e in json.loads(kw["messages"][0]["content"].split("\n", 1)[1])]
            return type(
                "R",
                (),
                {
                    "parsed_output": ScoreBatch(
                        scores=[
                            ScoredEvent(
                                id=i,
                                score=2,
                                reason="crypto-only; not a fit",
                                kind="networking",
                                level="unclear",
                            )
                            for i in ids
                        ]
                    )
                },
            )()

    def test_earlier_batches_keep_claudes_scores(self):
        events = self._events()
        scores = score_events(events, self.CONFIG, self.FailsOnBatch())
        survived = [e for e in events if not scores[e.key].degraded]
        assert [e.external_id for e in survived] == ["e0", "e1"]
        assert all(scores[e.key].score == 2.0 for e in survived)
        assert all(scores[e.key].reason == "crypto-only; not a fit" for e in survived)

    def test_earlier_batches_keep_claudes_kind(self):
        """`classify_kind` used to overwrite the kind Claude assigned."""
        events = self._events()
        score_events(events, self.CONFIG, self.FailsOnBatch())
        assert [e.kind for e in events[:2]] == ["networking", "networking"]

    def test_only_the_failed_batch_is_degraded(self):
        events = self._events()
        scores = score_events(events, self.CONFIG, self.FailsOnBatch())
        degraded = sorted(e.external_id for e in events if scores[e.key].degraded)
        assert degraded == ["e2", "e3"]

    def test_a_later_batch_still_runs_after_an_earlier_one_fails(self):
        """`continue`, not `break`: batch 3 must still be attempted."""
        events = self._events(6)
        client = self.FailsOnBatch(fail_on=1)
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 3
        degraded = sorted(e.external_id for e in events if scores[e.key].degraded)
        assert degraded == ["e0", "e1"]

    def test_rejected_events_are_not_queued_by_a_later_batch_failure(self, tmp_path):
        """The end-to-end harm: Claude said 2/10, the fallback said 8.0, and
        `_select` queued it."""
        events = self._events()
        scores = score_events(events, self.CONFIG, self.FailsOnBatch())
        store = Store(tmp_path / "t.db")
        selected = _select(events, scores, store, self.CONFIG, 99, dry_run=False)
        seen = sorted(e.external_id for e in events if store.is_seen(e))
        store.close()
        assert selected == []
        assert seen == ["e0", "e1"], "Claude's rejections should be final and recorded"

    def test_an_auth_failure_still_aborts_every_batch(self):
        """A bad key is bad for all of them; there is nothing to salvage, and
        `score_events` keeps its existing handling."""
        import anthropic

        exc = anthropic.AuthenticationError(
            "bad key",
            response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
            body=None,
        )
        events = self._events()
        client = self.FailsOnBatch(fail_on=2, exc=exc)
        scores = score_events(events, self.CONFIG, client)
        assert all(scores[e.key].degraded for e in events)
        assert client.calls == 2, "should abort, not keep trying every batch"

    def test_the_failure_names_the_batch_in_the_log(self, caplog):
        with caplog.at_level("ERROR"):
            score_events(self._events(), self.CONFIG, self.FailsOnBatch())
        assert any("batch 2 of 2 failed" in r.getMessage() for r in caplog.records)


class TestDegradedEventsNeverReachTheQueue:
    """PROJECT.md promises "degraded scoring is never final". `store.queue_event`
    writes the same `events` table `is_seen` reads, so a queued event is as
    final as a recorded one -- the promise has to cover passes, not just skips.

    The examples are the reviewer's: off-profile events that clear the base bar
    on classification alone once KIND_SIGNAL_BONUS is applied.
    """

    OFF_PROFILE = [
        ("Solana Grizzlython", "devpost"),
        ("Crypto Trading League", "luma"),
        ("DeFi Yield Farming Jam", "luma"),
    ]

    def _real_interests(self):

        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            return tomllib.load(f)["interests"]

    @pytest.mark.parametrize("title,source", OFF_PROFILE)
    def test_an_outage_does_not_queue_them(self, tmp_path, title, source):
        interests = self._real_interests()
        config = {"interests": interests, "scoring": {}, "notify": {}}
        event = make_event(title=title, source=source)
        scores = score_events([event], config, _Explodes())
        assert scores[event.key].degraded is True

        store = Store(tmp_path / "t.db")
        selected = _select([event], scores, store, config, 99, dry_run=False)
        seen = store.is_seen(event)
        store.close()
        assert selected == [], f"{title!r} was queued for posting on a degraded run"
        assert seen is False, f"{title!r} was buried without Claude ever seeing it"

    def test_a_whole_fetch_is_deferred_not_queued(self, tmp_path):
        """The scale of it: one 529 could queue up to max_per_day unvetted
        events, which then drip out over the following hours."""
        interests = self._real_interests()
        config = {"interests": interests, "scoring": {}, "notify": {}}
        events = [
            make_event(title=f"Crypto Trading League {i}", source="luma", external_id=str(i))
            for i in range(15)
        ]
        scores = score_events(events, config, _Explodes())
        store = Store(tmp_path / "t.db")
        selected = _select(events, scores, store, config, cap=15, dry_run=False)
        seen = [e for e in events if store.is_seen(e)]
        store.close()
        assert selected == []
        assert seen == []

    def test_ingest_queues_nothing_during_an_outage(self, tmp_path, monkeypatch):
        """Through `_ingest`, since that is what actually calls queue_event."""
        interests = self._real_interests()
        config = {
            "interests": interests,
            "scoring": {},
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }
        events = [make_event(title=t, source=s) for t, s in self.OFF_PROFILE]
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: _Explodes())
        store = Store(tmp_path / "t.db")
        cli._ingest(config, store)
        depth = store.queue_depth()
        store.close()
        assert depth == 0, "an outage queued unvetted events"


# ---------------------------------------------------------------------------
# An outage's backlog must not be buried at the cap on recovery
# ---------------------------------------------------------------------------


class TestOutageBacklogIsSpreadAcrossRuns:
    """Deferral moves the cost of an outage to the first healthy run. Every
    degraded run leaves its events unrecorded, so they pile up; when Claude
    comes back they all arrive at once, and `_select` used to record everything
    past `max_per_day` as seen -- burying good events for no better reason than
    their place in a queue.
    """

    CAP = 3

    def _config(self):
        return {
            **CONFIG,
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": self.CAP},
        }

    def _events(self, n=5):
        return [make_event(title=f"AI Hackathon {i}", external_id=str(i)) for i in range(n)]

    def _ingest(self, monkeypatch, store, events, client):
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: client)
        cli._ingest(self._config(), store)

    def test_recovery_queues_up_to_the_cap_and_holds_the_rest(self, tmp_path, monkeypatch):
        events = self._events()
        store = Store(tmp_path / "t.db")
        for _ in range(2):  # a sustained outage, not a single blip
            self._ingest(monkeypatch, store, events, _Explodes())
        assert store.queue_depth() == 0

        self._ingest(monkeypatch, store, events, _ScoresEverything(9, "hackathon"))
        depth = store.queue_depth()
        unseen = [e for e in events if not store.is_seen(e)]
        store.close()
        assert depth == self.CAP
        assert len(unseen) == len(events) - self.CAP, "the over-cap backlog was buried"

    def test_the_held_events_are_queued_by_later_runs(self, tmp_path, monkeypatch):
        events = self._events()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, events, _Explodes())
        healthy = _ScoresEverything(9, "hackathon")
        self._ingest(monkeypatch, store, events, healthy)
        self._ingest(monkeypatch, store, events, healthy)
        depth = store.queue_depth()
        unseen = [e for e in events if not store.is_seen(e)]
        store.close()
        assert depth == len(events), "part of the backlog never came back"
        assert unseen == []

    def test_a_held_event_claude_rejects_is_recorded_and_released(self, tmp_path, monkeypatch):
        """Being held is not a pass. Once Claude scores it low it is final like
        any other reject, and the deferral marker goes with it."""
        events = self._events()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, events, _Explodes())
        assert all(store.was_deferred(e) for e in events)

        self._ingest(monkeypatch, store, events, _ScoresEverything(2, "hackathon"))
        seen = [store.is_seen(e) for e in events]
        still_marked = [store.was_deferred(e) for e in events]
        depth = store.queue_depth()
        store.close()
        assert all(seen) and depth == 0
        assert not any(still_marked)

    def test_events_no_outage_touched_are_still_recorded_over_the_cap(self, tmp_path, monkeypatch):
        """The guardrail this must not weaken: a lost or evicted DB makes every
        event look new, and recording the over-cap ones is what turns that into
        one bounded day of posts instead of a replay of the whole backlog."""
        events = self._events()
        store = Store(tmp_path / "t.db")
        healthy = _ScoresEverything(9, "hackathon")
        self._ingest(monkeypatch, store, events, healthy)
        assert store.queue_depth() == self.CAP
        assert all(store.is_seen(e) for e in events)

        self._ingest(monkeypatch, store, events, healthy)
        depth = store.queue_depth()
        store.close()
        assert depth == self.CAP, "over-cap events came back without ever being deferred"

    def test_a_dry_run_marks_nothing_as_deferred(self, tmp_path):
        event = make_event(title="AI Hackathon")
        store = Store(tmp_path / "t.db")
        scores = {event.key: ScoreResult(9.0, "x", degraded=True)}
        _select([event], scores, store, CONFIG, 99, dry_run=True)
        marked = store.was_deferred(event)
        store.close()
        assert marked is False

    def test_the_marker_survives_reopening_the_database(self, tmp_path):
        """Outage and recovery are separate runs, hence separate processes."""
        event = make_event(title="AI Hackathon")
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        store.mark_deferred(event)  # idempotent
        store.close()
        reopened = Store(tmp_path / "t.db")
        marked, seen = reopened.was_deferred(event), reopened.is_seen(event)
        reopened.close()
        assert marked is True
        assert seen is False, "a deferred event must stay re-scorable"

    @pytest.mark.parametrize("decide", ["record", "queue_event"])
    def test_deciding_an_event_clears_its_marker(self, tmp_path, decide):
        event = make_event(title="AI Hackathon")
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        getattr(store, decide)(event, 9.0, "x")
        marked = store.was_deferred(event)
        store.close()
        assert marked is False
