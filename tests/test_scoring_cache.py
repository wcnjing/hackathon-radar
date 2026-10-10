"""The content-addressed score cache in `scoring.py`: what it stores, how it
survives a damaged or outdated file, and where it lives."""

import json
import os
from pathlib import Path

import pytest
from scoring_fakes import CONFIG, _ScoresEverything, make_event

from hackathon_radar import scoring
from hackathon_radar.config import env_flag
from hackathon_radar.scoring import (
    CACHE_SCHEMA_VERSION,
    ScoreBatch,
    ScoredEvent,
    _cache_enabled,
    _cache_key,
    _cache_load,
    _cache_read,
    _cache_store,
    score_events,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A cache row that no longer parses must read as a miss
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
# A short answer must not be frozen as the batch's response
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
# The cache file is rewritten atomically
# ---------------------------------------------------------------------------


class TestCacheWriteIsAtomic:
    def test_write_leaves_no_temp_file_behind(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        _cache_store({}, "k", [{"id": "a"}])
        assert cache_file.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_the_file_is_replaced_not_rewritten_in_place(self, tmp_path, monkeypatch):
        """The inode is the evidence. An in-place truncate-and-write keeps it;
        os.replace swaps in a different file and so changes it. Asserting on
        size or content instead would pass against plain write_text, which is
        exactly the bug this guards against."""
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        _cache_store({}, "first", [{"id": "a"}])
        inode_before = cache_file.stat().st_ino
        _cache_store(_cache_load(), "second", [{"id": "b"}])
        assert set(json.loads(cache_file.read_text(encoding="utf-8"))) == {"first", "second"}
        assert cache_file.stat().st_ino != inode_before, (
            "cache was rewritten in place rather than replaced"
        )

    def test_os_replace_is_what_publishes_the_new_cache(self, tmp_path, monkeypatch):
        """Belt and braces for platforms where st_ino is not meaningful: assert
        the call itself, and that it moves a temp file onto CACHE_PATH."""
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        calls = []
        real_replace = os.replace
        monkeypatch.setattr(
            "hackathon_radar.scoring.os.replace",
            lambda src, dst: (calls.append((str(src), str(dst))), real_replace(src, dst))[1],
        )
        _cache_store({}, "k", [{"id": "a"}])
        assert len(calls) == 1, "the cache was written without os.replace"
        src, dst = calls[0]
        assert dst == str(cache_file)
        assert src != dst and src.endswith(".tmp")

    def test_a_failed_publish_leaves_the_previous_cache_intact(self, tmp_path, monkeypatch):
        """The guarantee, not the mechanism. write_text truncates first, so an
        interrupted write destroys what was there; write-then-rename cannot."""
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        _cache_store({}, "first", [{"id": "a"}])

        def boom(src, dst):
            raise OSError("interrupted before publish")

        monkeypatch.setattr("hackathon_radar.scoring.os.replace", boom)
        _cache_store(_cache_load(), "second", [{"id": "b"}])  # must not raise

        surviving = json.loads(cache_file.read_text(encoding="utf-8"))
        assert set(surviving) == {"first"}, "an interrupted write damaged the existing cache"

    def test_eviction_still_applies(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_LIMIT", 3)
        cache = {}
        for i in range(5):
            _cache_store(cache, f"k{i}", [{"id": str(i)}])
        assert len(cache) == 3
        assert "k0" not in cache and "k4" in cache

    def test_unwritable_directory_warns_rather_than_raising(self, tmp_path, monkeypatch, caplog):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the cache directory should be", encoding="utf-8")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", blocker / "c.json")
        with caplog.at_level("WARNING"):
            _cache_store({}, "k", [{"id": "a"}])  # must not raise
        assert any("could not write score cache" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# A cache file holding valid JSON of the wrong shape is a cold cache
# ---------------------------------------------------------------------------


class TestCacheFileShape:
    """`_cache_load` caught unparseable files but returned whatever a parseable
    one held. A list or a null then raised AttributeError on the first lookup,
    which escaped into `score_events`' blanket handler and marked every event
    degraded: nothing posted, nothing recorded, no API call made, on every run.
    """

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            '["a", "b"]',
            '"a string"',
            "null",
            "42",
            "3.5",
            "true",
            "",
            "{not json",
            "\x00\x01",
        ],
    )
    def test_anything_but_an_object_loads_as_an_empty_cache(self, tmp_path, monkeypatch, content):
        cache_file = tmp_path / "c.json"
        cache_file.write_text(content, encoding="utf-8")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        assert _cache_load() == {}

    def test_a_missing_file_loads_as_an_empty_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "absent.json")
        assert _cache_load() == {}

    def test_an_object_still_loads(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "c.json"
        cache_file.write_text('{"k": []}', encoding="utf-8")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        assert _cache_load() == {"k": []}

    @pytest.mark.parametrize("content", ["[]", "null", '"a string"', "42"])
    def test_a_wrong_shape_does_not_degrade_the_run(self, tmp_path, monkeypatch, caplog, content):
        """End to end: Claude is healthy, so its score must be used and the
        event must not be deferred. The file heals on the next write."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        cache_file = tmp_path / "c.json"
        cache_file.write_text(content, encoding="utf-8")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)

        event = make_event(title="AI Student Hackathon", source="devpost", external_id="e1")
        client = _ScoresEverything(score=9, kind="hackathon")
        with caplog.at_level("WARNING"):
            result = score_events([event], CONFIG, client)[event.key]

        assert result.score == 9.0, "fell back to the keyword scorer"
        assert result.degraded is False
        assert client.calls == 1
        assert not [r for r in caplog.records if "scoring call failed" in r.getMessage()]
        healed = json.loads(cache_file.read_text(encoding="utf-8"))
        assert isinstance(healed, dict) and len(healed) == 1

    @pytest.mark.parametrize("rows", ["abc", 5, None, [5], [None], {"id": "x"}])
    def test_an_entry_of_the_wrong_shape_reads_as_a_miss(self, rows):
        """The same guarantee one level down: a good file with a bad entry."""
        assert _cache_read({"k": rows}, "k") is None


# ---------------------------------------------------------------------------
# Eviction is least-recently-used, not oldest-written
# ---------------------------------------------------------------------------


class TestCacheEvictionIsLRU:
    def _rows(self, i):
        return [dict(id=f"test:{i}", score=5, reason="r", kind="hackathon", level="unclear")]

    def _filled(self, tmp_path, monkeypatch, limit=3):
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_LIMIT", limit)
        cache = {}
        for i in range(limit):
            _cache_store(cache, f"k{i}", self._rows(i))
        return cache

    def test_a_hit_protects_an_old_entry_from_eviction(self, tmp_path, monkeypatch):
        cache = self._filled(tmp_path, monkeypatch)
        assert _cache_read(cache, "k0") is not None  # oldest written, just used
        _cache_store(cache, "k3", self._rows(3))
        assert "k0" in cache, "the entry just read was evicted for being written first"
        assert "k1" not in cache, "the least recently used entry should have gone"
        assert set(cache) == {"k0", "k2", "k3"}

    def test_without_a_hit_the_oldest_still_goes(self, tmp_path, monkeypatch):
        cache = self._filled(tmp_path, monkeypatch)
        _cache_store(cache, "k3", self._rows(3))
        assert set(cache) == {"k1", "k2", "k3"}

    def test_recency_survives_a_reload(self, tmp_path, monkeypatch):
        """The order is only worth anything if the file carries it."""
        cache = self._filled(tmp_path, monkeypatch)
        _cache_read(cache, "k0")
        _cache_store(cache, "k3", self._rows(3))
        reloaded = _cache_load()
        _cache_store(reloaded, "k4", self._rows(4))
        assert set(reloaded) == {"k0", "k3", "k4"}

    def test_a_miss_or_an_unreadable_entry_does_not_reorder(self, tmp_path, monkeypatch):
        cache = self._filled(tmp_path, monkeypatch)
        cache["k0"] = [{"id": "broken"}]
        assert _cache_read(cache, "k0") is None
        assert _cache_read(cache, "absent") is None
        assert list(cache) == ["k0", "k1", "k2"]

    def test_score_events_refreshes_the_batch_it_reuses(self, tmp_path, monkeypatch):
        """Through the real path: a batch served from cache outlives a batch
        that was written later but never asked for again."""
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", tmp_path / "c.json")
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_LIMIT", 2)
        client = _ScoresEverything(score=9, kind="hackathon")

        def score(tag):
            score_events([make_event(title=f"AI Hackathon {tag}", external_id=tag)], CONFIG, client)

        score("a")
        score("b")
        score("a")  # hit: "a" is now the most recently used
        score("c")  # evicts "b"
        assert client.calls == 3
        score("a")
        assert client.calls == 3, "the batch in active use was evicted"
        score("b")
        assert client.calls == 4


# ---------------------------------------------------------------------------
# RADAR_NO_SCORE_CACHE: anything a person would mean as "on"
# ---------------------------------------------------------------------------


class TestCacheBypassFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON", " 1 "])
    def test_truthy_values_bypass_the_cache(self, monkeypatch, value):
        monkeypatch.setenv("RADAR_NO_SCORE_CACHE", value)
        assert env_flag("RADAR_NO_SCORE_CACHE") is True
        assert _cache_enabled() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "  "])
    def test_falsy_values_leave_it_on(self, monkeypatch, value):
        monkeypatch.setenv("RADAR_NO_SCORE_CACHE", value)
        assert _cache_enabled() is True

    def test_unset_leaves_it_on(self, monkeypatch):
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        assert _cache_enabled() is True

    def test_yes_really_does_re_ask_the_model_and_write_nothing(self, tmp_path, monkeypatch):
        """`=yes` used to be silently ignored: the flag looked set and the
        cache kept answering."""
        cache_file = tmp_path / "c.json"
        monkeypatch.setattr("hackathon_radar.scoring.CACHE_PATH", cache_file)
        monkeypatch.setenv("RADAR_NO_SCORE_CACHE", "yes")
        client = _ScoresEverything(score=9, kind="hackathon")
        for _ in range(2):
            score_events([make_event(title="AI Hackathon")], CONFIG, client)
        assert client.calls == 2
        assert not cache_file.exists()


# ---------------------------------------------------------------------------
# Where the cache lives
# ---------------------------------------------------------------------------

# Captured at import, before conftest's autouse fixture repoints CACHE_PATH.
DEFAULT_CACHE_PATH = scoring.CACHE_PATH


class TestCacheLocation:
    def test_the_default_is_outside_the_directory_ci_persists(self):
        """radar.yml saves and restores `data/` on every run. The cache almost
        never hits on that path, so keeping it there was pure transfer cost."""
        assert PROJECT_ROOT / "data" not in DEFAULT_CACHE_PATH.parents

    def test_the_default_is_gitignored(self):
        relative = DEFAULT_CACHE_PATH.relative_to(PROJECT_ROOT)
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").split()
        assert f"{relative.parts[0]}/" in ignored

    def test_the_env_var_moves_it(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "somewhere" / "else" / "scores.json"
        monkeypatch.setenv("RADAR_SCORE_CACHE_PATH", str(elsewhere))
        _cache_store({}, "k", [{"id": "a"}])
        assert elsewhere.exists(), "parent directories were not created"
        assert not scoring.CACHE_PATH.exists(), "the default location was written as well"
        assert _cache_load() == {"k": [{"id": "a"}]}

    def test_a_blank_env_var_falls_back_to_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RADAR_SCORE_CACHE_PATH", "  ")
        _cache_store({}, "k", [{"id": "a"}])
        assert scoring.CACHE_PATH.exists()

    def test_score_events_honours_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)
        elsewhere = tmp_path / "moved.json"
        monkeypatch.setenv("RADAR_SCORE_CACHE_PATH", str(elsewhere))
        client = _ScoresEverything(score=9, kind="hackathon")
        for _ in range(2):
            score_events([make_event(title="AI Hackathon")], CONFIG, client)
        assert client.calls == 1
        assert elsewhere.exists()
