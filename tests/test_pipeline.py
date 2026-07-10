import argparse
from datetime import datetime, timedelta, timezone

from hackathon_radar.filtering import (
    KEYWORD_REASON_PREFIX,
    in_scope,
    keyword_score,
    normalize_title,
)
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
        assert '<a href="https://example.com/register">Register here</a>' in msg

    def test_minimal_event(self):
        msg = format_message(make_event())
        assert "Some Hackathon" in msg
        assert "Register here" not in msg  # no register link when none is known
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
