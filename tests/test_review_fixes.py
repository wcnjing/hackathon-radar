"""Regression coverage for the code review of the #4 / #8 branch.

Each class here maps to a numbered review finding. The critical one is
`TestDegradedRunsAreRevisitable`: the review's central objection was that a
transient API error permanently buried a whole fetch, because `_select` recorded
guessed scores into the table `is_seen` reads and nothing ever re-scored them.
"""

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from hackathon_radar import cli
from hackathon_radar.cli import _select
from hackathon_radar.filtering import (
    classify_kind,
    classify_kind_with_signal,
)
from hackathon_radar.models import Event
from hackathon_radar.scoring import (
    CACHE_SCHEMA_VERSION,
    ScoreBatch,
    ScoredEvent,
    ScoreResult,
    _cache_key,
    _cache_load,
    _cache_read,
    _cache_store,
    score_events,
)
from hackathon_radar.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]

INTERESTS = {
    "keywords": ["ai", "meetup", "networking", "hackathon", "student"],
    "min_score": 6,
    "min_score_by_kind": {"networking": 8},
}
CONFIG = {"interests": INTERESTS, "scoring": {"batch_size": 20}, "notify": {}}


def make_event(title="Some Hackathon", source="test", external_id="1", **kw) -> Event:
    return Event(
        source=source, external_id=external_id, title=title, url="https://example.com", **kw
    )


class _Explodes:
    """Stands in for a transient 529."""

    class messages:
        @staticmethod
        def parse(*a, **k):
            raise RuntimeError("transient 529 overloaded")


class _AuthFails:
    class messages:
        @staticmethod
        def parse(*a, **k):
            import anthropic

            raise anthropic.AuthenticationError(
                "bad key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None,
            )


class _ScoresEverything:
    """A healthy client: returns one score per event, all of them low."""

    def __init__(self, score=2, kind="networking"):
        self.score, self.kind = score, kind
        self.calls = 0
        self.messages = self

    def parse(self, **kw):
        self.calls += 1
        ids = [e["id"] for e in json.loads(kw["messages"][0]["content"].split("\n", 1)[1])]
        batch = ScoreBatch(
            scores=[
                ScoredEvent(id=i, score=self.score, reason="r", kind=self.kind, level="unclear")
                for i in ids
            ]
        )
        return type("R", (), {"parsed_output": batch})()


# ---------------------------------------------------------------------------
# Finding 1 (Critical): degraded scores must not be recorded as final
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

    def test_over_cap_on_a_degraded_run_is_also_left_unrecorded(self, tmp_path):
        events = [make_event(title=f"AI Hackathon {i}", external_id=str(i)) for i in range(3)]
        scores = {e.key: ScoreResult(9.0, "good", degraded=True) for e in events}
        store = Store(tmp_path / "t.db")
        selected = _select(events, scores, store, CONFIG, cap=1, dry_run=False)
        unseen = [e for e in events if not store.is_seen(e)]
        store.close()
        assert len(selected) == 1
        assert len(unseen) == 3, "capped events on a degraded run must stay re-scorable"

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
# Finding 2: a cache row that no longer parses must read as a miss
# ---------------------------------------------------------------------------


class TestCacheSchemaDrift:
    def _row(self, **over):
        row = dict(id="test:1", score=9, reason="good", kind="hackathon", level="unclear")
        row.update(over)
        return row

    def test_a_row_missing_a_field_reads_as_a_miss(self, caplog):
        stale = self._row()
        del stale["level"]  # exactly what adding/renaming a ScoredEvent field does
        with caplog.at_level("WARNING"):
            assert _cache_read({"k": [stale]}, "k") is None
        assert any("unreadable score-cache entry" in r.message for r in caplog.records)

    def test_a_row_with_an_extra_field_still_reads(self):
        """Removing a field from ScoredEvent leaves old rows carrying one it no
        longer knows. Pydantic ignores extras, and that is the right call here:
        the surviving fields are still the model's real answer. The dangerous
        direction -- a row *missing* a field -- is covered above, and
        CACHE_SCHEMA_VERSION busts every key either way."""
        rows = _cache_read({"k": [self._row(brand_new_field=1)]}, "k")
        assert rows is not None and rows[0].score == 9

    def test_a_row_with_a_bad_enum_reads_as_a_miss(self):
        assert _cache_read({"k": [self._row(kind="conference")]}, "k") is None

    def test_a_good_row_still_reads(self):
        rows = _cache_read({"k": [self._row()]}, "k")
        assert rows is not None and rows[0].score == 9

    def test_absent_key_reads_as_a_miss(self):
        assert _cache_read({}, "nope") is None

    def test_drift_does_not_degrade_the_run_to_keyword_scoring(self, tmp_path, monkeypatch):
        """The review's demonstration: a ValidationError used to escape into
        `score_events`' blanket handler, log "Claude scoring failed", and send
        the whole run to the keyword scorer over a deserialization bug."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)

        event = make_event(title="AI Student Hackathon", source="devpost", external_id="e1")
        client = _ScoresEverything(score=9, kind="hackathon")
        fresh = score_events([event], CONFIG, client)
        assert fresh[event.key].score == 9.0

        # Simulate a ScoredEvent schema change against an existing cache.
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        for rows in cache.values():
            for row in rows:
                row.pop("level", None)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")

        after = score_events(
            [make_event(title="AI Student Hackathon", source="devpost", external_id="e1")],
            CONFIG,
            client,
        )
        result = next(iter(after.values()))
        assert result.score == 9.0, "drift silently fell back to the keyword scorer"
        assert result.degraded is False

    def test_schema_version_participates_in_the_key(self, monkeypatch):
        before = _cache_key("m", "sys", "payload")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_SCHEMA_VERSION", "999")
        assert _cache_key("m", "sys", "payload") != before

    def test_schema_version_is_a_string(self):
        assert isinstance(CACHE_SCHEMA_VERSION, str)


# ---------------------------------------------------------------------------
# Finding 4: a short answer must not be frozen as the batch's response
# ---------------------------------------------------------------------------


class TestPartialBatchIsNotCached:
    def _events(self, n=3):
        return [make_event(title=f"AI Hackathon {i}", external_id=str(i)) for i in range(n)]

    class ShortAnswer:
        """Returns a score for only the first event in the batch."""

        def __init__(self):
            self.calls = 0
            self.messages = self

        def parse(self, **kw):
            self.calls += 1
            ids = [e["id"] for e in json.loads(kw["messages"][0]["content"].split("\n", 1)[1])]
            batch = ScoreBatch(
                scores=[
                    ScoredEvent(id=ids[0], score=9, reason="r", kind="hackathon", level="unclear")
                ]
            )
            return type("R", (), {"parsed_output": batch})()

    def test_short_answer_is_not_written_to_the_cache(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        with caplog.at_level("WARNING"):
            score_events(self._events(), CONFIG, self.ShortAnswer())
        assert not cache_file.exists() or json.loads(cache_file.read_text()) == {}
        assert any("not caching this batch" in r.message for r in caplog.records)

    def test_the_model_is_asked_again_next_run(self, tmp_path, monkeypatch):
        """The point of not caching: a bad answer must not become permanent."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        client = self.ShortAnswer()
        score_events(self._events(), CONFIG, client)
        score_events(self._events(), CONFIG, client)
        assert client.calls == 2

    def test_a_complete_answer_is_cached(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        client = _ScoresEverything(score=9, kind="hackathon")
        score_events(self._events(), CONFIG, client)
        score_events(self._events(), CONFIG, client)
        assert client.calls == 1, "a complete answer should have been served from cache"
        assert len(json.loads(cache_file.read_text(encoding="utf-8"))) == 1

    def test_missing_events_still_get_a_score(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        events = self._events()
        results = score_events(events, CONFIG, self.ShortAnswer())
        assert len(results) == 3
        assert sum(r.degraded for r in results.values()) == 2


# ---------------------------------------------------------------------------
# Finding 6: the cache file is rewritten atomically
# ---------------------------------------------------------------------------


class TestCacheWriteIsAtomic:
    def test_write_leaves_no_temp_file_behind(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        _cache_store({}, "k", [{"id": "a"}])
        assert cache_file.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_an_existing_cache_is_never_truncated_in_place(self, tmp_path, monkeypatch):
        """os.replace swaps the file; a reader sees the old or the new one."""
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        _cache_store({}, "first", [{"id": "a"}])
        inode_before = cache_file.stat().st_size
        cache = _cache_load()
        _cache_store(cache, "second", [{"id": "b"}])
        assert set(json.loads(cache_file.read_text(encoding="utf-8"))) == {"first", "second"}
        assert cache_file.stat().st_size != inode_before

    def test_eviction_still_applies(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_LIMIT", 3)
        cache = {}
        for i in range(5):
            _cache_store(cache, f"k{i}", [{"id": str(i)}])
        assert len(cache) == 3
        assert "k0" not in cache and "k4" in cache

    def test_unwritable_directory_warns_rather_than_raising(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(
            "hackathon_radar.scoring.CACHE_PATH", tmp_path / "no" / "such" / "dir" / "c.json"
        )
        with caplog.at_level("WARNING"):
            _cache_store({}, "k", [{"id": "a"}])  # must not raise


# ---------------------------------------------------------------------------
# Finding 3 + 8: classification uses every available signal, and anchors properly
# ---------------------------------------------------------------------------


class TestClassifyKind:
    @pytest.mark.parametrize(
        "title,expected",
        [
            # the review's confirmed misclassifications
            ("Social Impact Hackathon 2026", "hackathon"),
            ("Hackathon Demo Day", "hackathon"),
            ("AI Workshop: Build an Agent", "hackathon"),
            ("Antler Residency", "program"),
            # anchoring: "sprint" must not fire inside "Sprinter"
            ("Sprinter Van Expo", "networking"),
            # still-correct classifications that must not regress
            ("Founders Networking Meetup", "networking"),
            ("AI Builders Mixer", "networking"),
            ("Fireside Chat with a Founder", "networking"),
            ("Startup Accelerator 2026", "program"),
            ("NUS Datathon 2026", "hackathon"),
            ("Hack&Roll 2026", "hackathon"),
            ("GenAI Buildathon", "hackathon"),
            ("Global Game Jam", "hackathon"),
            ("AWS DeepRacer Student League", "hackathon"),
            ("AI Singapore Apprenticeship Programme", "program"),
        ],
    )
    def test_titles(self, title, expected):
        assert classify_kind(make_event(title=title)) == expected

    @pytest.mark.parametrize("source", ["devpost", "mlh"])
    def test_hackathon_only_sources_win_over_the_title(self, source):
        """MHacks and TartanHacks carry no token a regex can safely anchor on,
        but the source they came from only ever publishes hackathons."""
        assert classify_kind(make_event(title="MHacks 2026", source=source)) == "hackathon"
        assert classify_kind(make_event(title="TartanHacks", source=source)) == "hackathon"

    def test_tags_are_searched_as_well_as_the_title(self):
        """MLH stamps every event with this tag; Devpost carries its themes."""
        event = make_event(title="Spring Season Kickoff", tags=["student hackathon"])
        assert classify_kind(event) == "hackathon"

    def test_location_is_not_searched(self):
        """A venue called "The Hive Social" is not a networking event."""
        event = make_event(title="GenAI Buildathon", location="The Hive Social, Singapore")
        assert classify_kind(event) == "hackathon"

    def test_hackathon_beats_networking_when_a_title_carries_both(self):
        assert classify_kind(make_event(title="Hackathon Mixer and Demo Day")) == "hackathon"

    def test_ambiguous_titles_report_no_signal(self):
        kind, matched = classify_kind_with_signal(make_event(title="Tuesday Evening"))
        assert (kind, matched) == ("networking", False)

    def test_matched_titles_report_a_signal(self):
        assert classify_kind_with_signal(make_event(title="AI Buildathon"))[1] is True
        assert classify_kind_with_signal(make_event(title="Founders Meetup"))[1] is True

    @pytest.mark.parametrize(
        "title", ["Sprinter Van Expo", "Shack Party", "Jamboree Festival", "Hackney Community Day"]
    )
    def test_word_boundaries_prevent_false_hackathons(self, title):
        assert classify_kind(make_event(title=title)) != "hackathon"


class TestKindSignalBonus:
    """A confident build-event classification counts as one keyword hit, so the
    classifier regexes and `interests.keywords` cannot silently drift apart."""

    def test_a_datathon_no_keyword_knows_still_clears_the_bar(self):
        event = make_event(title="NUS Datathon 2026")
        result = score_events([event], CONFIG, None)[event.key]
        assert event.kind == "hackathon"
        assert result.score >= INTERESTS["min_score"], "classified but scored as irrelevant"

    def test_networking_gets_no_bonus(self):
        plain = make_event(title="Tuesday Evening", external_id="1")
        mixer = make_event(title="Founders Networking Mixer", external_id="2")
        scores = score_events([plain, mixer], CONFIG, None)
        assert mixer.kind == "networking"
        # the mixer's extra points come from keyword hits only, never a bonus
        assert "networking title" not in scores[mixer.key].reason

    def test_the_bonus_is_recorded_in_the_reason(self):
        event = make_event(title="GenAI Buildathon")
        result = score_events([event], CONFIG, None)[event.key]
        assert "+hackathon title" in result.reason

    def test_the_bonus_never_pushes_past_ten(self):
        event = make_event(title="AI Student Hackathon Meetup Networking", location="Singapore")
        result = score_events([event], CONFIG, None)[event.key]
        assert result.score <= 10.0

    def test_an_unsignalled_event_gets_no_bonus(self):
        event = make_event(title="Tuesday Evening")
        result = score_events([event], CONFIG, None)[event.key]
        assert "+" not in result.reason


# ---------------------------------------------------------------------------
# Finding 5: guard the acceptance criterion against the REAL config.toml
# ---------------------------------------------------------------------------


class TestAgainstTheRealConfig:
    """Issue #4's AC1 says "under default config". Hand-written test interests
    can drift from config.toml without CI noticing, so assert on the shipped
    file itself."""

    @pytest.fixture(scope="class")
    @classmethod
    def real(cls):
        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            return tomllib.load(f)["interests"]

    def _outcome(self, event, interests):
        result = score_events([event], {"interests": interests, "scoring": {}}, None)[event.key]
        threshold = interests.get("min_score_by_kind", {}).get(
            event.kind, interests.get("min_score", 6)
        )
        return result.score >= threshold, event.kind, result.score

    def test_the_shipped_config_still_raises_the_networking_bar(self, real):
        """If this ever stops being true the rest of the class proves nothing."""
        assert real["min_score_by_kind"]["networking"] > real["min_score"]

    @pytest.mark.parametrize(
        "title",
        [
            "Founders Networking Meetup",  # the issue's own example
            "AI Builders Mixer & Demo Day",
            "Startup Founders' Breakfast",
            "Fireside Chat with a Startup Founder",
            "Tech Talk: Scaling to a Million Users",
        ],
    )
    def test_networking_events_do_not_post_under_the_shipped_config(self, real, title):
        posts, kind, score = self._outcome(make_event(title=title, location="Singapore"), real)
        assert kind == "networking", f"{title!r} classified {kind!r}"
        assert not posts, f"{title!r} posted at {score}"

    @pytest.mark.parametrize(
        "title",
        [
            # every title the review named as wrongly blocked
            "AI Agents Hackathon 2026",
            "Anthropic Builder Workshop Singapore",
            "Women Who Code Workshop: LLM Fine-tuning",
            "AI Singapore Apprenticeship Programme",
            "AWS DeepRacer Student League",
            "Build with AI: Gemini Developer Day",
            "NUS Datathon 2026",
            "Hack&Roll 2026",
            # and a few more of the same shape
            "GenAI Buildathon SG",
            "MHacks 2026",
            "Social Impact Hackathon 2026",
            "Antler Residency",
        ],
    )
    def test_wanted_events_still_post_under_the_shipped_config(self, real, title):
        """The review's Critical finding was that the fix blocked 16 of 17
        events the profile explicitly asks for. Guard the other direction too."""
        posts, kind, score = self._outcome(make_event(title=title, location="Singapore"), real)
        assert posts, f"{title!r} blocked at {score} as {kind!r}"

    def test_the_build_boundary_separates_building_from_mingling(self, real):
        """`build` is what lets "Build with AI" through. Its closing
        boundary is what keeps "AI Builders Mixer" out. Both halves matter, so
        both are asserted together -- widening one silently breaks the other."""
        builds, _, _ = self._outcome(make_event(title="Build with AI: Gemini Day"), real)
        mingles, kind, score = self._outcome(make_event(title="AI Builders Mixer & Demo Day"), real)
        assert builds is True
        assert mingles is False, f"a mixer posted at {score} as {kind!r}"


# ---------------------------------------------------------------------------
# Finding 11: one taxonomy, imported by both users
# ---------------------------------------------------------------------------


class TestKindTaxonomyIsShared:
    def test_scored_event_uses_the_filtering_alias(self):
        from typing import get_args

        from hackathon_radar.filtering import Kind

        assert set(get_args(ScoredEvent.model_fields["kind"].annotation)) == set(get_args(Kind))

    def test_classify_kind_only_returns_taxonomy_members(self):
        from typing import get_args

        from hackathon_radar.filtering import Kind

        titles = ["Hackathon", "Meetup", "Accelerator", "Tuesday Evening", "Sprinter Van"]
        assert {classify_kind(make_event(title=t)) for t in titles} <= set(get_args(Kind))


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
