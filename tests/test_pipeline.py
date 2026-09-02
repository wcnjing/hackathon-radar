import argparse
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from hackathon_radar.filtering import (
    KEYWORD_REASON_PREFIX,
    classify_kind,
    in_scope,
    keyword_score,
    normalize_title,
)
from hackathon_radar.scoring import ScoreBatch, ScoredEvent, score_events
from hackathon_radar.models import Event
from hackathon_radar.notify import Telegram, TelegramError, format_message, is_quiet_hour
from hackathon_radar.store import Store

SCOPE = {"mode": "sg_plus_online", "home_country": "SG", "home_city": "singapore"}


def make_event(**overrides) -> Event:
    defaults = dict(
        source="test", external_id="1", title="Some Hackathon", url="https://example.com"
    )
    defaults.update(overrides)
    return Event(**defaults)


class TestScope:
    def test_online_event_kept(self):
        assert in_scope(make_event(online=True, location="Online"), SCOPE)

    def test_sg_event_kept(self):
        assert in_scope(make_event(location="Singapore"), SCOPE)
        assert in_scope(make_event(country="SG", location="NUS"), SCOPE)

    def test_foreign_in_person_dropped(self):
        assert not in_scope(make_event(location="Chennai, Tamilnadu", country="IN"), SCOPE)

    def test_sg_only_drops_online(self):
        sg_only = {**SCOPE, "mode": "sg_only"}
        assert not in_scope(make_event(online=True, location="Online"), sg_only)
        assert in_scope(make_event(location="Singapore"), sg_only)

    def test_global_keeps_everything(self):
        assert in_scope(make_event(location="Chennai", country="IN"), {**SCOPE, "mode": "global"})


class TestKeywordScore:
    INTERESTS = {"keywords": ["ai", "student", "startup"]}

    def test_matching_event_scores_higher(self):
        ai = make_event(title="AI Agents Hackathon", tags=["student hackathon"])
        boring = make_event(title="Knitting Meetup")
        ai_score, ai_reason = keyword_score(ai, self.INTERESTS)
        boring_score, boring_reason = keyword_score(boring, self.INTERESTS)
        assert ai_score > boring_score
        # reasons are DB debug detail, marked so the notifier can hide them
        assert ai_reason.startswith(KEYWORD_REASON_PREFIX)
        assert boring_reason.startswith(KEYWORD_REASON_PREFIX)

    def test_score_capped_at_ten(self):
        event = make_event(title="ai student startup", tags=["ai", "student", "startup"])
        score, _ = keyword_score(event, self.INTERESTS)
        assert score <= 10

    def test_no_substring_false_positives(self):
        # "ai" must not match inside words like "trainocate" or "sustainability"
        event = make_event(title="Sustainability Workshop", location="TRAINOCATE Pte Ltd")
        score, reason = keyword_score(event, self.INTERESTS)
        assert reason == KEYWORD_REASON_PREFIX + "none matched"


class TestStore:
    def test_dedupe_roundtrip(self, tmp_path):
        store = Store(tmp_path / "test.db")
        event = make_event()
        assert not store.is_seen(event)
        store.record(event, 7.0, "test")
        assert store.is_seen(event)
        # recording again is a no-op, not an error
        store.record(event, 9.0, "again")
        store.mark_notified(event)
        store.close()

    def test_different_sources_are_distinct(self, tmp_path):
        store = Store(tmp_path / "test.db")
        store.record(make_event(source="devpost", external_id="42"), 5.0, "")
        assert not store.is_seen(make_event(source="mlh", external_id="42"))
        store.close()

    def test_queue_roundtrip_orders_by_score(self, tmp_path):
        store = Store(tmp_path / "test.db")
        low = make_event(external_id="low", title="Low")
        high = make_event(external_id="high", title="High", brief="the <brief> survives")
        store.queue_event(low, 6.0, "ok")
        store.queue_event(high, 9.0, "great")
        assert store.queue_depth() == 2

        event, score, reason = store.pop_queued()
        assert event.external_id == "high" and score == 9.0
        assert event.brief == "the <brief> survives"  # full payload roundtrips
        store.mark_notified(event)

        assert store.pop_queued()[0].external_id == "low"
        store.close()

    def test_queued_titles_lists_unsent_events(self, tmp_path):
        store = Store(tmp_path / "test.db")
        first = make_event(external_id="first", title="First")
        second = make_event(external_id="second", title="Second")
        store.queue_event(first, 8.0, "ok")
        store.queue_event(second, 7.0, "ok")
        assert store.queued_titles() == ["First", "Second"]

        store.mark_notified(first)
        assert store.queued_titles() == ["Second"]
        store.close()

    def test_drop_queued_keeps_event_seen_without_counting_as_sent(self, tmp_path):
        store = Store(tmp_path / "test.db")
        event = make_event(external_id="stale", title="Stale Duplicate")
        store.queue_event(event, 8.0, "ok")

        store.drop_queued(event, "skipped duplicate")

        assert store.is_seen(event)
        assert store.queue_depth() == 0
        assert store.notified_count_since("2000-01-01T00:00:00+00:00") == 0
        store.close()

    def test_migration_from_pre_queue_schema(self, tmp_path):
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE events (source TEXT NOT NULL, external_id TEXT NOT NULL,"
            " title TEXT, url TEXT, first_seen TEXT, score REAL, reason TEXT,"
            " notified_at TEXT, PRIMARY KEY (source, external_id))"
        )
        conn.execute("INSERT INTO events (source, external_id, title) VALUES ('devpost','1','Old')")
        conn.commit()
        conn.close()

        store = Store(db)  # must add payload column + meta table, not crash
        assert store.is_seen(make_event(source="devpost", external_id="1"))
        assert store.queue_depth() == 0
        store.queue_event(make_event(external_id="2"), 7.0, "r")
        assert store.queue_depth() == 1
        store.close()

    def test_meta_roundtrip(self, tmp_path):
        store = Store(tmp_path / "test.db")
        assert store.get_meta("last_send_at") is None
        store.set_meta("last_send_at", "2026-07-10T00:00:00+00:00")
        store.set_meta("last_send_at", "2026-07-10T01:00:00+00:00")
        assert store.get_meta("last_send_at") == "2026-07-10T01:00:00+00:00"
        store.close()


class TestDryRun:
    def test_dry_run_does_not_mark_events_seen(self, tmp_path, monkeypatch, capsys):
        """Regression: a dry run must not persist events, or the first real
        run would silently skip everything the dry run previewed."""
        from hackathon_radar import cli

        event = make_event(online=True, title="AI Hackathon")
        config = {
            "interests": {"keywords": ["ai"], "min_score": 1},
            "scope": {"mode": "global"},
            "notify": {"max_per_run": 5},
        }
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        monkeypatch.setattr(
            cli,
            "score_events",
            lambda events, cfg, client=None: {e.key: (9.0, "great fit") for e in events},
        )

        args = argparse.Namespace(dry_run=True, max_notify=None)
        assert cli.run(args) == 0
        assert "would queue" in capsys.readouterr().out

        store = Store(tmp_path / "radar.db")
        assert not store.is_seen(event)
        store.close()


class TestFormatMessage:
    def test_html_escaped_and_complete(self):
        event = make_event(
            title="Hack <World> & Co",
            dates_text="Aug 1 - 3, 2026",
            time_left="26 days left",
            location="Singapore",
            prize="$10,000",
            tags=["AI", "Web"],
            register_url="https://example.com/register",
            invite_only=True,
            organizer="Acme Labs",
            team_size="solo or teams up to 5",
            brief="Build an AI agent that does <cool> things.",
            deadline="register by Aug 2, 2026",
        )
        msg = format_message(event)
        assert "Hack &lt;World&gt; &amp; Co" in msg
        assert "<World>" not in msg
        assert "Aug 1 - 3, 2026" in msg
        assert "⏳ 26 days left" in msg
        assert "$10,000" in msg
        assert "👥 solo or teams up to 5" in msg
        assert "🔒 Invite only" in msg
        assert "⏰ register by Aug 2, 2026" in msg
        assert "👤 Acme Labs" in msg
        # scores and relevance reasons are backend-only; cards stay public-friendly
        assert "/10" not in msg
        assert "💡" not in msg
        assert "<blockquote expandable>Build an AI agent that does &lt;cool&gt; things.</blockquote>" in msg
        assert 'href="https://example.com"' in msg
        assert "Register here" not in msg
        assert "https://example.com/register" not in msg

    def test_minimal_event(self):
        msg = format_message(make_event())
        assert "Some Hackathon" in msg
        assert "Register here" not in msg
        assert "🔒" not in msg
        assert "👥" not in msg
        assert "blockquote" not in msg

    def test_blank_line_after_title(self):
        msg = format_message(make_event(dates_text="Aug 1"))
        title_line, blank, rest = msg.split("\n", 2)
        assert "Some Hackathon" in title_line
        assert blank == ""

    def test_kind_picks_emoji(self):
        assert format_message(make_event(kind="hackathon")).startswith("🛠")
        assert format_message(make_event(kind="networking")).startswith("🤝")
        assert format_message(make_event(kind="program")).startswith("🚀")
        # unknown kinds fall back rather than crash
        assert format_message(make_event(kind="mystery")).startswith("🛠")

    def test_experience_level_line(self):
        assert "🌱 Beginner friendly" in format_message(make_event(level="beginner"))
        assert "⚡ Some experience helpful" in format_message(make_event(level="intermediate"))
        assert "🔥 Advanced / competitive" in format_message(make_event(level="advanced"))
        # unclear level → no badge (no badge beats a wrong badge)
        msg = format_message(make_event(level=None))
        assert "🌱" not in msg and "🔥" not in msg


class TestSpamGuards:
    def test_normalize_title(self):
        assert normalize_title("AI Wednesdays #42 — July Edition!") == "ai wednesdays 42 july edition"
        assert normalize_title("  AI   Wednesdays #43  ") != ""

    def test_quiet_hours_wrap_midnight(self):
        assert is_quiet_hour(23, 23, 8)
        assert is_quiet_hour(2, 23, 8)
        assert not is_quiet_hour(8, 23, 8)
        assert not is_quiet_hour(12, 23, 8)
        assert is_quiet_hour(10, 9, 12)  # non-wrapping window
        assert not is_quiet_hour(3, 5, 5)  # start == end disables

    def test_store_daily_count_and_titles(self, tmp_path):
        store = Store(tmp_path / "t.db")
        a = make_event(external_id="a", title="AI Meetup")
        b = make_event(external_id="b", title="Never Sent")
        store.record(a, 8.0, "")
        store.record(b, 8.0, "")
        store.mark_notified(a)
        assert store.notified_count_since("2000-01-01T00:00:00+00:00") == 1
        assert store.notified_titles_since("2000-01-01T00:00:00+00:00") == ["AI Meetup"]
        assert store.notified_count_since("2999-01-01T00:00:00+00:00") == 0
        store.close()

    def test_same_title_notified_once_per_run(self, tmp_path, monkeypatch, capsys):
        """Cross-source duplicates (or weekly reposts) collapse to one card."""
        from hackathon_radar import cli

        devpost_ev = make_event(source="devpost", external_id="1", title="AI Agents Jam!")
        luma_ev = make_event(source="luma", external_id="2", title="ai agents jam")
        config = {
            "interests": {"keywords": [], "min_score": 1},
            "scope": {"mode": "global"},
            "notify": {"max_per_run": 5},
        }
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [devpost_ev, luma_ev])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        monkeypatch.setattr(
            cli,
            "score_events",
            lambda events, cfg, client=None: {e.key: (9.0, "fit") for e in events},
        )

        assert cli.run(argparse.Namespace(dry_run=True, max_notify=None)) == 0
        assert capsys.readouterr().out.count("would queue") == 1


class TestFullEvents:
    def _run(self, tmp_path, monkeypatch, events, scope):
        from hackathon_radar import cli

        config = {"interests": {"min_score": 1}, "scope": scope, "notify": {"max_per_run": 9}}
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: None)
        monkeypatch.setattr(
            cli, "score_events", lambda evs, cfg, client=None: {e.key: (9.0, "x") for e in evs}
        )
        cli.run(argparse.Namespace(dry_run=True, max_notify=None))

    def test_full_event_dropped_by_default(self, tmp_path, monkeypatch, capsys):
        open_ev = make_event(external_id="1", title="Open Event")
        full_ev = make_event(external_id="2", title="Full Event", full=True)
        self._run(tmp_path, monkeypatch, [open_ev, full_ev], {"mode": "global"})
        out = capsys.readouterr().out
        assert "Open Event" in out
        assert "Full Event" not in out

    def test_include_full_opt_in(self, tmp_path, monkeypatch, capsys):
        full_ev = make_event(external_id="2", title="Full Event", full=True)
        self._run(tmp_path, monkeypatch, [full_ev], {"mode": "global", "include_full": True})
        assert "Full Event" in capsys.readouterr().out


class TestKindThreshold:
    def test_networking_needs_higher_score(self, tmp_path, monkeypatch, capsys):
        """min_score_by_kind: a 7/10 hackathon posts, a 7/10 meetup doesn't."""
        from hackathon_radar import cli

        hack = make_event(external_id="1", title="Decent Hackathon", kind="hackathon")
        meetup = make_event(external_id="2", title="Decent Meetup", kind="networking")
        great_meetup = make_event(external_id="3", title="Anthropic Night", kind="networking")
        config = {
            "interests": {"min_score": 6, "min_score_by_kind": {"networking": 8}},
            "scope": {"mode": "global"},
            "notify": {"max_per_run": 9},
            "enrich": {"enabled": False},
        }
        scores = {hack.key: (7.0, "x"), meetup.key: (7.0, "x"), great_meetup.key: (9.0, "x")}
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [hack, meetup, great_meetup])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        monkeypatch.setattr(cli, "score_events", lambda evs, cfg, client=None: scores)

        assert cli.run(argparse.Namespace(dry_run=True, max_notify=None)) == 0
        out = capsys.readouterr().out
        assert "Decent Hackathon" in out
        assert "Anthropic Night" in out
        assert "Decent Meetup" not in out


class TestEnrichmentScope:
    def test_enrichment_only_touches_notified_events(self, tmp_path, monkeypatch, capsys):
        """Cost guard: enrichment (paid per-event page calls) must run only for
        events that clear the score threshold and cap, not every new event."""
        from hackathon_radar import cli

        good = make_event(source="devpost", external_id="1", title="AI Hackathon")
        weak = make_event(source="devpost", external_id="2", title="Meh Event")
        config = {
            "interests": {"min_score": 6},
            "scope": {"mode": "global"},
            "notify": {"max_per_run": 5},
            "enrich": {"enabled": True},
        }
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [good, weak])
        monkeypatch.setattr(cli, "make_client", lambda: object())
        monkeypatch.setattr(
            cli,
            "score_events",
            lambda events, cfg, client=None: {good.key: (9.0, "great"), weak.key: (2.0, "weak")},
        )
        enriched = []
        monkeypatch.setattr(cli, "enrich_events", lambda evs, cfg, client: enriched.extend(evs))

        assert cli.run(argparse.Namespace(dry_run=True, max_notify=None)) == 0
        # only the event that will actually be posted was enriched
        assert [e.title for e in enriched] == ["AI Hackathon"]


class TestDripQueue:
    def _wire(self, monkeypatch, tmp_path, events, telegram):
        from hackathon_radar import cli

        config = {
            "interests": {"min_score": 1},
            "scope": {"mode": "global"},
            "notify": {"max_per_day": 15, "send_interval_seconds": 1800, "fetch_every_hours": 6},
            "enrich": {"enabled": False},
        }
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "Telegram", lambda: telegram)
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: events)
        monkeypatch.setattr(cli, "make_client", lambda: None)
        monkeypatch.setattr(
            cli, "score_events", lambda evs, cfg, client=None: {e.key: (9.0, "x") for e in evs}
        )
        return cli

    def test_one_post_per_run_until_gap_elapses(self, tmp_path, monkeypatch):
        sent = []

        class FakeTelegram:
            configured = True

            def send(self, text, silent=False):
                sent.append(text)

        events = [make_event(external_id=str(i), title=f"Event {i}") for i in range(3)]
        cli = self._wire(monkeypatch, tmp_path, events, FakeTelegram())
        args = argparse.Namespace(dry_run=False, max_notify=None)

        assert cli.run(args) == 0  # ingest queues 3, drips exactly 1
        assert len(sent) == 1
        store = Store(tmp_path / "radar.db")
        assert store.queue_depth() == 2

        assert cli.run(args) == 0  # 30-min gap not elapsed → nothing sent
        assert len(sent) == 1

        past = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds")
        store.set_meta("last_send_at", past)  # pretend last post was 31 min ago
        store.close()
        assert cli.run(args) == 0
        assert len(sent) == 2

    def test_failed_send_stays_queued(self, tmp_path, monkeypatch):
        class FailingTelegram:
            configured = True

            def send(self, text, silent=False):
                raise TelegramError("Telegram sendMessage failed: whatever")

        events = [make_event(external_id="1", title="Event 1")]
        cli = self._wire(monkeypatch, tmp_path, events, FailingTelegram())

        assert cli.run(argparse.Namespace(dry_run=False, max_notify=None)) == 1
        store = Store(tmp_path / "radar.db")
        assert store.queue_depth() == 1  # still queued — retried next run
        assert store.notified_count_since("2000-01-01T00:00:00+00:00") == 0
        store.close()

    def test_queued_title_blocks_later_duplicate(self, tmp_path, monkeypatch):
        sent = []

        class FakeTelegram:
            configured = True

            def send(self, text, silent=False):
                sent.append(text)

        first = make_event(source="devpost", external_id="1", title="AI Agents Jam!")
        duplicate = make_event(source="luma", external_id="2", title="ai agents jam")
        batches = iter([[first], [duplicate]])
        cli = self._wire(monkeypatch, tmp_path, [], FakeTelegram())
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: next(batches))
        args = argparse.Namespace(dry_run=False, max_notify=None)

        assert cli.run(args) == 0
        store = Store(tmp_path / "radar.db")
        assert store.queue_depth() == 0  # first event was queued, then dripped
        store.set_meta("last_fetch_at", "2000-01-01T00:00:00+00:00")
        store.set_meta("last_send_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.queue_event(make_event(source="watchlist", external_id="queued", title="AI Agents Jam"), 8.0, "x")
        store.close()

        assert cli.run(args) == 0
        store = Store(tmp_path / "radar.db")
        assert store.queue_depth() == 1
        assert store.is_seen(duplicate)
        assert store.queued_titles() == ["AI Agents Jam"]
        store.close()

    def test_drain_drops_stale_queued_duplicate_before_sending(self, tmp_path, monkeypatch):
        sent = []

        class FakeTelegram:
            configured = True

            def send(self, text, silent=False):
                sent.append(text)

        cli = self._wire(monkeypatch, tmp_path, [], FakeTelegram())
        store = Store(tmp_path / "radar.db")
        already_sent = make_event(source="devpost", external_id="old", title="AI Agents Jam!")
        stale_duplicate = make_event(source="luma", external_id="new", title="ai agents jam")
        fresh = make_event(source="mlh", external_id="fresh", title="Fresh Buildathon")
        store.record(already_sent, 9.0, "x")
        store.mark_notified(already_sent)
        store.queue_event(stale_duplicate, 9.0, "x")
        store.queue_event(fresh, 8.0, "x")
        store.set_meta("last_fetch_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.set_meta("last_send_at", "2000-01-01T00:00:00+00:00")
        store.close()

        assert cli.run(argparse.Namespace(dry_run=False, max_notify=None)) == 0
        assert len(sent) == 1
        assert "Fresh Buildathon" in sent[0]
        store = Store(tmp_path / "radar.db")
        assert store.queue_depth() == 0
        assert store.is_seen(stale_duplicate)
        assert store.notified_count_since("2000-01-01T00:00:00+00:00") == 2
        store.close()


class TestTelegramErrors:
    def test_error_carries_description_but_never_the_token(self, monkeypatch):
        """Regression: httpx's raise_for_status quoted the request URL, which
        embeds the bot token — errors must stay token-free."""
        import pytest

        from hackathon_radar import notify

        class FakeResponse:
            status_code = 403

            def json(self):
                return {"ok": False, "description": "Forbidden: bot is not a member"}

        monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: FakeResponse())
        telegram = Telegram(token="123456:SECRETTOKENVALUE", chat_id="@chan")
        with pytest.raises(TelegramError) as excinfo:
            telegram.send("hello")
        assert "Forbidden: bot is not a member" in str(excinfo.value)
        assert "SECRETTOKENVALUE" not in str(excinfo.value)


class TestIssue4KeywordFallbackKind:
    """Issue #4: the keyword fallback must not sneak networking events past the
    raised `min_score_by_kind` bar. Regression coverage for every degraded path:
    no client, an auth failure, and any other exception during Claude scoring."""

    INTERESTS = {
        # the real config.toml keyword list, trimmed to what these titles hit
        "keywords": ["ai", "meetup", "networking", "founder", "hackathon", "student"],
        "min_score": 6,
        "min_score_by_kind": {"networking": 8},
    }
    CONFIG = {"interests": INTERESTS, "scoring": {"batch_size": 20}}

    class _AuthFails:
        class messages:
            @staticmethod
            def parse(*a, **k):
                import anthropic
                raise anthropic.AuthenticationError(
                    "bad key", response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                    body=None,
                )

    class _Explodes:
        class messages:
            @staticmethod
            def parse(*a, **k):
                raise RuntimeError("transient 529 overloaded")

    def _posts(self, event, score):
        """Mirror cli._select's gate: does this score clear the kind's bar?"""
        threshold = self.INTERESTS["min_score_by_kind"].get(
            event.kind, self.INTERESTS["min_score"]
        )
        return score >= threshold

    def _degraded_clients(self):
        return [("no client", None), ("auth error", self._AuthFails()),
                ("other exception", self._Explodes())]

    def test_founders_networking_meetup_never_posts_on_any_degraded_path(self):
        """The issue's own example: 'Founders Networking Meetup' scores 8.0 from
        the keyword scorer, which used to clear the base threshold of 6 because
        kind defaulted to 'hackathon'."""
        for label, client in self._degraded_clients():
            event = make_event(title="Founders Networking Meetup", location="Singapore")
            scores = score_events([event], self.CONFIG, client)
            score, reason = scores[event.key]
            assert event.kind == "networking", f"{label}: kind was {event.kind!r}"
            assert not self._posts(event, score), f"{label}: posted at {score}"

    def test_genuine_hackathon_still_posts_on_degraded_paths(self):
        """The cap must not punish events that aren't networking."""
        for label, client in self._degraded_clients():
            event = make_event(title="AI Student Hackathon", location="Singapore")
            scores = score_events([event], self.CONFIG, client)
            score, _ = scores[event.key]
            assert event.kind == "hackathon", f"{label}: kind was {event.kind!r}"
            assert self._posts(event, score), f"{label}: blocked at {score}"

    def test_claude_success_path_is_untouched(self):
        """Acceptance criterion 3: when Claude scores, its kind and score win —
        a networking event it rates 9 still posts, and no cap is applied."""
        event = make_event(title="Founders Networking Meetup")

        class _Scores:
            class messages:
                @staticmethod
                def parse(*a, **k):
                    scored = ScoredEvent(
                        id="test:1", score=9, reason="rare access to major builders",
                        kind="networking", level="unclear",
                    )
                    return SimpleNamespace(parsed_output=ScoreBatch(scores=[scored]))

        scores = score_events([event], self.CONFIG, _Scores())
        score, reason = scores[event.key]
        assert event.kind == "networking"
        assert score == 9.0
        assert reason == "rare access to major builders"
        assert self._posts(event, score)

    def test_events_claude_skips_are_capped_too(self):
        """A batch response that omits an event falls back per-event."""
        skipped = make_event(external_id="2", title="Generic Tech Meetup")

        class _ReturnsNothing:
            class messages:
                @staticmethod
                def parse(*a, **k):
                    return SimpleNamespace(parsed_output=ScoreBatch(scores=[]))

        scores = score_events([skipped], self.CONFIG, _ReturnsNothing())
        score, _ = scores[skipped.key]
        assert skipped.kind == "networking"
        assert not self._posts(skipped, score)

    def test_classify_kind_tiers(self):
        assert classify_kind(make_event(title="NUS Hackers Buildathon")) == "hackathon"
        assert classify_kind(make_event(title="Founders' Breakfast")) == "networking"
        assert classify_kind(make_event(title="Y Combinator Fellowship")) == "program"
        # ambiguous titles default to networking, the stricter bar
        assert classify_kind(make_event(title="Untitled Thing")) == "networking"

    def test_end_to_end_meetup_not_posted_when_scoring_degrades(self, tmp_path, monkeypatch, capsys):
        """Acceptance criterion 1, through the real pipeline: with no Anthropic
        client, a meetup is recorded but never printed as a would-post card."""
        from hackathon_radar import cli

        meetup = make_event(external_id="1", title="Founders Networking Meetup",
                            location="Singapore")
        hackathon = make_event(external_id="2", title="AI Student Hackathon",
                               location="Singapore")
        config = {
            "interests": self.INTERESTS,
            "scope": {"mode": "global"},
            "notify": {"max_per_run": 9},
            "enrich": {"enabled": False},
        }
        monkeypatch.setattr(cli, "load_config", lambda: config)
        monkeypatch.setattr(cli, "db_path", lambda: tmp_path / "radar.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [meetup, hackathon])
        monkeypatch.setattr(cli, "make_client", lambda: None)

        assert cli.run(argparse.Namespace(dry_run=True, max_notify=None)) == 0
        out = capsys.readouterr().out
        assert "AI Student Hackathon" in out
        assert "Founders Networking Meetup" not in out

    def test_competitions_are_not_mistaken_for_networking(self):
        """Competition vocabulary the README uses must land in the hackathon
        tier, or the ambiguous->networking default silently raises their bar."""
        for title in [
            "OpenCV AI Competition 2026, powered by AWS",
            "SPEED August AI Challenge",
            "NUS Datathon 2026",
            "Optiver Ready Trader Go Contest",
        ]:
            assert classify_kind(make_event(title=title)) == "hackathon", title

    def test_word_boundaries_hold(self):
        """The tier regexes are anchored: no substring false positives."""
        # "jam" must not fire inside "pyjamas"; "hack" is a prefix match by design
        assert classify_kind(make_event(title="Pyjamas Party")) == "networking"
        assert classify_kind(make_event(title="Hackathon Kickoff")) == "hackathon"


class TestIssue8ScoringDeterminism:
    """Issue #8: scoring must be reproducible run to run, or no prompt change
    can be told apart from sampling noise (and #10's calibration harness has
    nothing stable to measure against)."""

    INTERESTS = {"profile": "test profile", "keywords": ["ai"], "min_score": 6}
    CONFIG = {"interests": INTERESTS, "scoring": {"model": "claude-haiku-4-5", "batch_size": 3}}

    class RecordingClient:
        """Scores every event 7/10 and records the kwargs of each call."""

        def __init__(self):
            self.calls = []
            outer = self

            class messages:
                @staticmethod
                def parse(**kwargs):
                    outer.calls.append(kwargs)
                    payload = kwargs["messages"][0]["content"]
                    ids = re.findall(r'"id": "([^"]+)"', payload)
                    return SimpleNamespace(parsed_output=ScoreBatch(scores=[
                        ScoredEvent(id=i, score=7, reason="stable reason",
                                    kind="hackathon", level="unclear")
                        for i in ids
                    ]))

            self.messages = messages

    def _events(self, n=7):
        return [
            make_event(source="devpost", external_id=f"e{i}", title=f"AI Hackathon {i}")
            for i in range(n)
        ]

    def test_temperature_zero_is_sent_on_every_call(self):
        """The core fix: remove the sampling randomness we can control. Measured
        residual after this change is 93% modal agreement (server-side, not
        sampling) — see the note on SCORING_TEMPERATURE."""
        client = self.RecordingClient()
        score_events(self._events(), self.CONFIG, client)
        assert client.calls, "expected at least one model call"
        for call in client.calls:
            assert call["temperature"] == 0.0, f"temperature was {call.get('temperature')!r}"

    def test_batch_composition_is_independent_of_fetch_order(self):
        """Same events, different arrival order -> identical batches. Without
        this, temperature=0 alone still lets an event score differently, because
        it would be prompted alongside a different set of neighbours."""
        events = self._events()
        shuffled = list(reversed(events))

        def batches_for(evs):
            client = self.RecordingClient()
            score_events(evs, self.CONFIG, client)
            return [re.findall(r'"id": "([^"]+)"', c["messages"][0]["content"])
                    for c in client.calls]

        assert batches_for(events) == batches_for(shuffled)

    class NeighbourSensitiveClient:
        """Models the effect the issue warns about: an event shares one prompt
        with the rest of its batch, so its score depends on the neighbours it is
        scored alongside. Here the score is the event's position in its batch —
        if batch composition is unstable, the same event scores differently."""

        def __init__(self):
            class messages:
                @staticmethod
                def parse(**kwargs):
                    ids = re.findall(r'"id": "([^"]+)"', kwargs["messages"][0]["content"])
                    return SimpleNamespace(parsed_output=ScoreBatch(scores=[
                        ScoredEvent(id=eid, score=5 + pos, reason="positional",
                                    kind="hackathon", level="unclear")
                        for pos, eid in enumerate(ids)
                    ]))

            self.messages = messages

    def test_repeated_scoring_of_a_fixed_batch_is_identical(self):
        """Acceptance criterion 1: the same set of events scores identically
        however it arrives. Uses a neighbour-sensitive stub, so this fails if
        batching depends on fetch order."""
        events = self._events()
        first = score_events(events, self.CONFIG, self.NeighbourSensitiveClient())
        second = score_events(list(reversed(self._events())), self.CONFIG,
                              self.NeighbourSensitiveClient())
        assert first == second

    def test_batch_size_partitions_all_events_exactly_once(self):
        """No event dropped or double-scored at a batch boundary (7 events / 3)."""
        client = self.RecordingClient()
        events = self._events(7)
        results = score_events(events, self.CONFIG, client)
        sent = [i for c in client.calls
                for i in re.findall(r'"id": "([^"]+)"', c["messages"][0]["content"])]
        assert len(client.calls) == 3           # 3 + 3 + 1
        assert len(sent) == len(set(sent)) == 7
        assert len(results) == 7

    def test_prompt_is_stable_across_runs(self):
        """The whole request, not just temperature, must be byte-identical —
        a drifting prompt would move scores as surely as sampling noise."""
        def payloads():
            client = self.RecordingClient()
            score_events(self._events(), self.CONFIG, client)
            return [(c["model"], c["system"], c["messages"][0]["content"],
                     c["temperature"]) for c in client.calls]

        assert payloads() == payloads()


class TestIssue8ScoringCache:
    """Issue #8 acceptance criterion 1: "scoring the same batch twice returns
    identical scores and kinds".

    temperature=0 cannot deliver this — measured 4/5 events stable, the residual
    being server-side floating-point on shared hardware, which no API parameter
    reaches. So determinism is bought at our layer: the batch request is
    content-addressed and its scores stored, making score_events a pure function
    of its input even though the model underneath is not."""

    INTERESTS = {"profile": "test profile", "keywords": ["ai"], "min_score": 6}
    CONFIG = {"interests": INTERESTS, "scoring": {"model": "claude-haiku-4-5", "batch_size": 20}}

    @pytest.fixture(autouse=True)
    def enable_cache(self, monkeypatch):
        monkeypatch.delenv("RADAR_NO_SCORE_CACHE", raising=False)

    class DriftingClient:
        """Returns a DIFFERENT score every call — the worst case the real model
        can present. If the cache works, only the first call is ever made."""

        def __init__(self):
            self.calls = 0
            outer = self

            class messages:
                @staticmethod
                def parse(**kwargs):
                    outer.calls += 1
                    ids = re.findall(r'"id": "([^"]+)"', kwargs["messages"][0]["content"])
                    return SimpleNamespace(parsed_output=ScoreBatch(scores=[
                        ScoredEvent(id=i, score=(outer.calls % 10), reason=f"call {outer.calls}",
                                    kind="networking" if outer.calls % 2 else "hackathon",
                                    level="unclear")
                        for i in ids
                    ]))

            self.messages = messages

    def _events(self):
        return [make_event(source="devpost", external_id=f"e{i}", title=f"AI Hackathon {i}")
                for i in range(3)]

    def test_same_batch_twice_returns_identical_scores_and_kinds(self):
        """The criterion, verbatim — against a client that would otherwise
        return something different every single time."""
        client = self.DriftingClient()

        first_events = self._events()
        first = score_events(first_events, self.CONFIG, client)
        second_events = self._events()
        second = score_events(second_events, self.CONFIG, client)

        assert first == second, "scores drifted between identical batches"
        assert [e.kind for e in first_events] == [e.kind for e in second_events]
        assert [e.level for e in first_events] == [e.level for e in second_events]
        assert client.calls == 1, f"second scoring re-asked the model ({client.calls} calls)"

    def test_cache_survives_a_new_process(self, tmp_path, monkeypatch):
        """Determinism must hold across runs, not just within one."""
        from hackathon_radar import scoring

        monkeypatch.setattr(scoring, "CACHE_PATH", tmp_path / "c.json")
        client = self.DriftingClient()
        first = score_events(self._events(), self.CONFIG, client)
        # a fresh client stands in for a new process; the cache file persists
        second = score_events(self._events(), self.CONFIG, self.DriftingClient())
        assert first == second
        assert client.calls == 1

    def test_changing_the_interest_profile_busts_the_cache(self):
        """A stale answer must never mask a change the founder actually made."""
        client = self.DriftingClient()
        score_events(self._events(), self.CONFIG, client)

        edited = {**self.CONFIG, "interests": {**self.INTERESTS, "profile": "a different taste"}}
        score_events(self._events(), edited, client)
        assert client.calls == 2, "profile edit reused a cached score"

    def test_different_events_are_not_served_a_cached_answer(self):
        client = self.DriftingClient()
        score_events(self._events(), self.CONFIG, client)
        other = [make_event(source="luma", external_id="x1", title="Totally Different Event")]
        score_events(other, self.CONFIG, client)
        assert client.calls == 2

    def test_cache_can_be_bypassed(self, monkeypatch):
        """RADAR_NO_SCORE_CACHE=1 asks the model what it thinks today."""
        monkeypatch.setenv("RADAR_NO_SCORE_CACHE", "1")
        client = self.DriftingClient()
        score_events(self._events(), self.CONFIG, client)
        score_events(self._events(), self.CONFIG, client)
        assert client.calls == 2

    def test_corrupt_cache_file_degrades_quietly(self, tmp_path, monkeypatch):
        """A truncated or hand-edited cache must not take the pipeline down."""
        from hackathon_radar import scoring

        bad = tmp_path / "corrupt.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(scoring, "CACHE_PATH", bad)

        client = self.DriftingClient()
        results = score_events(self._events(), self.CONFIG, client)
        assert len(results) == 3
        assert client.calls == 1
