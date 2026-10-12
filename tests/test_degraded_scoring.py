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
    degraded_scores,
    make_client,
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
        monkeypatch.setattr(cli, "make_client", lambda: None)
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

    def _auth_error(self):
        import anthropic

        return anthropic.AuthenticationError(
            "bad key",
            response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
            body=None,
        )

    def test_an_auth_failure_stops_calling_but_keeps_what_was_scored(self):
        """A bad key is bad for every later batch, so nothing more is sent. It
        says nothing about the batches already answered. This used to re-raise,
        and `score_events` then keyword-scored all of them, earlier ones too."""
        events = self._events(6)
        client = self.FailsOnBatch(fail_on=2, exc=self._auth_error())
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 2, "kept calling with credentials already rejected"
        kept = sorted(e.external_id for e in events if not scores[e.key].degraded)
        assert kept == ["e0", "e1"]
        assert all(scores[e.key].score == 2.0 for e in events[:2])
        assert all(scores[e.key].degraded for e in events[2:])

    def test_an_auth_failure_on_the_first_batch_degrades_everything(self):
        events = self._events()
        client = self.FailsOnBatch(fail_on=1, exc=self._auth_error())
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 1
        assert all(scores[e.key].degraded for e in events)

    def test_a_cache_hit_survives_a_later_auth_failure(self, tmp_path, monkeypatch):
        """The reviewer's first probe: batch 1 from the cache, batch 2 a 401.
        The cached 9/10 was replaced by a degraded keyword guess."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        first = self._events(2)
        score_events(first, self.CONFIG, _ScoresEverything(9, "hackathon"))  # warm batch 1

        events = self._events(4)
        client = self.FailsOnBatch(fail_on=1, exc=self._auth_error())
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 1
        assert [scores[e.key].score for e in events[:2]] == [9.0, 9.0]
        assert not any(scores[e.key].degraded for e in events[:2])
        assert all(scores[e.key].degraded for e in events[2:])

    def test_the_cache_still_answers_batches_after_an_auth_failure(self, tmp_path, monkeypatch):
        """Stopping API calls must not stop cache hits: batch 2 fails auth,
        batch 3 is already cached and needs no credentials."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        events = self._events(6)
        score_events(events[4:], self.CONFIG, _ScoresEverything(9, "hackathon"))  # warm batch 3

        client = self.FailsOnBatch(fail_on=2, exc=self._auth_error())
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 2
        degraded = sorted(e.external_id for e in events if scores[e.key].degraded)
        assert degraded == ["e2", "e3"]
        assert [scores[e.key].score for e in events[4:]] == [9.0, 9.0]

    def test_a_later_batch_with_no_parsed_output_keeps_earlier_scores(self):
        """The reviewer's second probe. The SDK types `parsed_output` as
        optional, and reading `.scores` off None happened outside the per-batch
        handler, so it took the whole run down with it."""

        class NoParsedOutputOnSecond(self.FailsOnBatch):
            def parse(inner, **kw):
                if inner.calls == 1:  # this is the second call
                    inner.calls += 1
                    return type("R", (), {"parsed_output": None})()
                return super().parse(**kw)

        events = self._events(6)
        client = NoParsedOutputOnSecond(fail_on=99)
        scores = score_events(events, self.CONFIG, client)
        assert client.calls == 3, "a batch after the bad one was not attempted"
        degraded = sorted(e.external_id for e in events if scores[e.key].degraded)
        assert degraded == ["e2", "e3"]
        assert all(scores[e.key].score == 2.0 for e in events[:2] + events[4:])

    def test_a_late_auth_failure_does_not_queue_or_bury_earlier_rejects(self, tmp_path):
        """End to end: Claude's 2/10 for batch 1 must stay a recorded reject,
        not turn into a deferred (or posted) keyword guess."""
        events = self._events()
        client = self.FailsOnBatch(fail_on=2, exc=self._auth_error())
        scores = score_events(events, self.CONFIG, client)
        store = Store(tmp_path / "t.db")
        selected = _select(events, scores, store, self.CONFIG, 99, dry_run=False)
        seen = sorted(e.external_id for e in events if store.is_seen(e))
        deferred = sorted(e.external_id for e in events if store.was_deferred(e))
        store.close()
        assert selected == []
        assert seen == ["e0", "e1"]
        assert deferred == ["e2", "e3"]

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


# ---------------------------------------------------------------------------
# "No credentials" and "could not build the client" are different outcomes
# ---------------------------------------------------------------------------


ANTHROPIC_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
)


class TestMakeClient:
    """`make_client` returns None when nothing is configured and raises when
    something is configured and unusable. Every exception used to become
    `client=None`, i.e. final keyword-only scoring."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for name in ANTHROPIC_ENV:
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)

    def _fake_sdk(self, monkeypatch, **attrs):
        """Stand in for the SDK's own credential lookup, which reads the
        developer's home directory and so cannot be exercised hermetically."""
        import anthropic

        built = type("FakeAnthropic", (), {"api_key": None, "auth_token": None, **attrs})
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: built())
        return built

    def test_no_credentials_at_all_is_none(self, monkeypatch):
        self._fake_sdk(monkeypatch, credentials=None)
        assert make_client() is None

    def test_an_empty_key_is_no_credentials(self, monkeypatch):
        """An unset Actions secret arrives as an empty string, which the SDK
        stores as-is."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        self._fake_sdk(monkeypatch, api_key="", credentials=None)
        assert make_client() is None

    def test_an_auth_token_is_a_client(self, monkeypatch):
        built = self._fake_sdk(monkeypatch, auth_token="token", credentials=None)
        assert isinstance(make_client(), built)

    def test_a_credentials_provider_is_a_client(self, monkeypatch):
        """An `ant auth login` profile or workload identity: no static key."""
        built = self._fake_sdk(monkeypatch, credentials=object())
        assert isinstance(make_client(), built)

    def test_an_sdk_without_a_credentials_attribute_still_works(self, monkeypatch):
        self._fake_sdk(monkeypatch)
        assert make_client() is None

    def test_an_api_key_is_a_real_client(self, monkeypatch):
        import anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        client = make_client()
        assert isinstance(client, anthropic.Anthropic)
        assert client.auth_token is None

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_a_key_with_a_broken_proxy_raises(self, monkeypatch):
        """The reviewer's example, against the real SDK: credentials are
        present and construction fails. That must not read as "no key"."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("HTTPS_PROXY", "not a proxy://::")
        with pytest.raises(Exception, match="proxy"):
            make_client()

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_a_key_with_a_broken_base_url_raises(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "::::")
        with pytest.raises(Exception):  # noqa: B017 - whatever httpx raises for it
            make_client()


class TestClientConstructionFailureIsDegraded:
    """Through `_ingest`: a configured deployment whose client cannot be built
    is having an outage. Its keyword guesses must neither post nor be recorded.
    """

    def _config(self):
        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            interests = tomllib.load(f)["interests"]
        return {
            "interests": interests,
            "scoring": {},
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }

    def _broken(self):
        raise ValueError("Unknown scheme for proxy URL")

    def _events(self):
        # One the keyword scorer would post and one it would bury.
        return [
            make_event(title="AI Agents Hackathon 2026", external_id="post"),
            make_event(title="Quantum Computing Primer", external_id="bury"),
        ]

    def test_premise_keyword_only_would_post_one_and_record_both(self, tmp_path, monkeypatch):
        events = self._events()
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: None)
        cli._ingest(self._config(), store)
        depth, seen = store.queue_depth(), [store.is_seen(e) for e in events]
        store.close()
        assert depth == 1 and seen == [True, True]

    def test_a_construction_failure_posts_and_records_nothing(self, tmp_path, monkeypatch, caplog):
        events = self._events()
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", self._broken)
        with caplog.at_level("WARNING"):
            cli._ingest(self._config(), store)
        depth = store.queue_depth()
        seen = [store.is_seen(e) for e in events]
        deferred = [store.was_deferred(e) for e in events]
        store.close()
        assert depth == 0, "a fallback guess posted while the client was broken"
        assert seen == [False, False], "a fallback guess was recorded permanently"
        assert deferred == [True, True]
        messages = [r.getMessage() for r in caplog.records]
        assert any("could not be initialised" in m and "DEGRADED" in m for m in messages)
        assert not any("No Anthropic credentials configured" in m for m in messages)

    def test_the_events_are_scored_properly_once_the_client_builds(self, tmp_path, monkeypatch):
        events = self._events()
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", self._broken)
        cli._ingest(self._config(), store)

        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        cli._ingest(self._config(), store)
        depth = store.queue_depth()
        store.close()
        assert depth == 2, "Claude rated both 9 once it was reachable"

    def test_missing_credentials_are_still_final_and_say_so(self, tmp_path, monkeypatch, caplog):
        events = self._events()
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: None)
        with caplog.at_level("WARNING"):
            cli._ingest(self._config(), store)
        deferred = [store.was_deferred(e) for e in events]
        store.close()
        messages = [r.getMessage() for r in caplog.records]
        assert deferred == [False, False]
        assert any("No Anthropic credentials configured" in m for m in messages)
        assert not any("DEGRADED" in m for m in messages)

    def test_a_dry_run_shows_nothing_from_a_broken_client(self, tmp_path, monkeypatch):
        events = self._events()
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", self._broken)
        new, scores, client = cli._collect(self._config(), store)
        selected = _select(new, scores, store, self._config(), 99, dry_run=True)
        store.close()
        assert client is None
        assert all(scores[e.key].degraded for e in events)
        assert selected == []

    def test_degraded_scores_flags_every_event(self):
        events = self._events()
        scores = degraded_scores(events, self._config())
        assert set(scores) == {e.key for e in events}
        assert all(r.degraded for r in scores.values())


class TestSourcesNeedAClient:
    """The two sources that extract with Claude skip on both outcomes."""

    def test_the_watchlist_skips_without_credentials(self, monkeypatch):
        from hackathon_radar.sources import watchlist

        monkeypatch.setattr("hackathon_radar.scoring.make_client", lambda: None)
        assert watchlist.fetch({"pages": ["https://x.org/"]}) == []

    def test_the_watchlist_skips_when_the_client_cannot_be_built(self, monkeypatch):
        from hackathon_radar.sources import watchlist

        def broken():
            raise ValueError("bad proxy")

        monkeypatch.setattr("hackathon_radar.scoring.make_client", broken)
        assert watchlist.fetch({"pages": ["https://x.org/"]}) == []

    def test_the_email_source_skips_without_credentials(self, monkeypatch):
        from hackathon_radar.sources import email_source

        monkeypatch.setenv("EMAIL_ADDRESS", "feed@example.com")
        monkeypatch.setenv("EMAIL_APP_PASSWORD", "app-password")
        monkeypatch.setattr("hackathon_radar.scoring.make_client", lambda: None)
        monkeypatch.setattr(
            email_source.imaplib,
            "IMAP4_SSL",
            lambda host: (_ for _ in ()).throw(AssertionError("opened a mailbox")),
        )
        assert email_source.fetch({}) == []


# ---------------------------------------------------------------------------
# A deferred event must come back even if its source never returns it again
# ---------------------------------------------------------------------------


class TestDeferredEventsRecoverWithoutTheirSource:
    """The deferred table used to hold keys only, so a retry still depended on
    the source handing the event over again. Sources that checkpoint before
    scoring (watchlist, email) never do, and their events stayed deferred
    forever. The whole payload is stored now and `_collect` carries it in.

    Here the source is simulated by a `fetch_all` that returns the event once;
    tests/test_watchlist.py and tests/test_email_source.py run the real ones.
    """

    CAP = 2

    def _config(self):
        return {
            **CONFIG,
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": self.CAP},
        }

    def _ingest(self, monkeypatch, store, fetched, client):
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: list(fetched))
        monkeypatch.setattr(cli, "make_client", lambda: client)
        cli._ingest(self._config(), store)

    def _rich_event(self):
        return make_event(
            title="AI Student Hackathon",
            source="watchlist",
            external_id="https://x.org/#ai student hackathon",
            dates_text="Nov 3-4",
            location="COM3, NUS",
            country="SG",
            online=True,
            tags=["ai", "student"],
            prize="$5,000",
            register_url="https://x.org/register",
        )

    def test_the_whole_event_round_trips_through_the_store(self, tmp_path):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        store.close()
        reopened = Store(tmp_path / "t.db")  # a later run is a later process
        restored = reopened.deferred_events()
        reopened.close()
        assert restored == [event]

    def test_a_source_that_never_returns_it_again_still_recovers(self, tmp_path, monkeypatch):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, [event], _Explodes())
        assert store.was_deferred(event) and store.queue_depth() == 0

        self._ingest(monkeypatch, store, [], _ScoresEverything(9, "hackathon"))
        depth, still_deferred = store.queue_depth(), store.was_deferred(event)
        queued = store.pop_queued()[0]
        store.close()
        assert depth == 1, "the event stayed deferred forever"
        assert still_deferred is False
        assert (queued.title, queued.url, queued.dates_text) == (
            event.title,
            event.url,
            event.dates_text,
        )

    def test_a_reject_on_recovery_is_recorded_and_released(self, tmp_path, monkeypatch):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, [event], _Explodes())
        self._ingest(monkeypatch, store, [], _ScoresEverything(2, "networking"))
        seen, left = store.is_seen(event), store.deferred_events()
        self._ingest(monkeypatch, store, [], _ScoresEverything(9, "hackathon"))
        depth = store.queue_depth()
        store.close()
        assert seen is True and left == []
        assert depth == 0, "a recorded reject was carried in and scored again"

    def test_it_keeps_being_carried_through_a_long_outage(self, tmp_path, monkeypatch):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, [event], _Explodes())
        for _ in range(3):
            self._ingest(monkeypatch, store, [], _Explodes())
        assert store.deferred_events() == [event]
        self._ingest(monkeypatch, store, [], _ScoresEverything(9, "hackathon"))
        depth = store.queue_depth()
        store.close()
        assert depth == 1

    def test_an_event_its_source_does_return_is_scored_once(self, tmp_path, monkeypatch):
        """Carried and fetched copies of one event must not both be scored."""
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, [event], _Explodes())
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [self._rich_event()])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        new, scores, _ = cli._collect(self._config(), store)
        store.close()
        assert len(new) == 1 and len(scores) == 1

    def test_the_fetched_copy_wins_over_the_stored_one(self, tmp_path, monkeypatch):
        """The page was edited during the outage; the stored payload is stale."""
        stale = self._rich_event()
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, [stale], _Explodes())
        fresh = self._rich_event()
        fresh.dates_text = "Nov 10-11 (postponed)"
        self._ingest(monkeypatch, store, [fresh], _ScoresEverything(9, "hackathon"))
        queued = store.pop_queued()[0]
        store.close()
        assert queued.dates_text == "Nov 10-11 (postponed)"

    def test_a_later_deferral_refreshes_the_stored_payload(self, tmp_path):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        event.dates_text = "Nov 10-11 (postponed)"
        store.mark_deferred(event)
        restored = store.deferred_events()
        store.close()
        assert [e.dates_text for e in restored] == ["Nov 10-11 (postponed)"]

    def test_a_backlog_held_at_the_cap_drains_without_its_source(self, tmp_path, monkeypatch):
        """Held events were retried only if re-fetched, which is the same bug
        one step later: recovery queued `cap` and stranded the rest."""
        events = [
            make_event(title=f"AI Hackathon {i}", source="email", external_id=f"<m{i}@x>#h")
            for i in range(5)
        ]
        store = Store(tmp_path / "t.db")
        self._ingest(monkeypatch, store, events, _Explodes())
        healthy = _ScoresEverything(9, "hackathon")
        depths = []
        for _ in range(3):
            self._ingest(monkeypatch, store, [], healthy)
            depths.append(store.queue_depth())
        left = store.deferred_events()
        store.close()
        assert depths == [2, 4, 5]
        assert left == []

    def test_scope_still_applies_to_a_carried_event(self, tmp_path, monkeypatch):
        event = make_event(title="AI Hackathon", country="US", location="New York")
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [])
        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        cli._ingest({**self._config(), "scope": {"mode": "sg_only"}}, store)
        depth = store.queue_depth()
        store.close()
        assert depth == 0

    def test_a_dry_run_previews_carried_events_and_changes_nothing(self, tmp_path, monkeypatch):
        event = self._rich_event()
        store = Store(tmp_path / "t.db")
        store.mark_deferred(event)
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [])
        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        new, scores, _ = cli._collect(self._config(), store)
        selected = _select(new, scores, store, self._config(), 99, dry_run=True)
        still, seen = store.was_deferred(event), store.is_seen(event)
        store.close()
        assert selected == [event]
        assert still is True and seen is False

    def test_a_key_only_row_from_the_first_schema_is_migrated(self, tmp_path):
        """The table shipped once without a payload column. Such a row cannot
        be carried, but it must not break the store or lose its marker."""
        import sqlite3

        path = tmp_path / "t.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE deferred (source TEXT NOT NULL, external_id TEXT NOT NULL,"
            " PRIMARY KEY (source, external_id))"
        )
        conn.execute("INSERT INTO deferred VALUES ('test', '1')")
        conn.commit()
        conn.close()

        event = make_event(title="AI Hackathon")
        store = Store(path)
        marked, carried = store.was_deferred(event), store.deferred_events()
        store.mark_deferred(event)  # deferred again: the payload is filled in
        refilled = store.deferred_events()
        store.close()
        assert marked is True and carried == []
        assert refilled == [event]

    @pytest.mark.parametrize("payload", ["{not json", "[]", '{"title": "no key fields"}', "null"])
    def test_an_unreadable_payload_is_skipped_not_raised(self, tmp_path, payload):
        good = make_event(title="AI Hackathon", external_id="good")
        store = Store(tmp_path / "t.db")
        store.conn.execute("INSERT INTO deferred VALUES ('test', 'bad', ?)", (payload,))
        store.mark_deferred(good)
        carried = store.deferred_events()
        store.close()
        assert carried == [good]
