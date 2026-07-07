import argparse

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
        assert "would notify" in capsys.readouterr().out

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
        msg = format_message(event, "Strong AI focus")
        assert "Hack &lt;World&gt; &amp; Co" in msg
        assert "<World>" not in msg
        assert "Aug 1 - 3, 2026" in msg
        assert "⏳ 26 days left" in msg
        assert "$10,000" in msg
        assert "👥 solo or teams up to 5" in msg
        assert "🔒 Invite only" in msg
        assert "⏰ register by Aug 2, 2026" in msg
        assert "👤 Acme Labs" in msg
        assert "💡 Strong AI focus" in msg
        assert "/10" not in msg  # scores are internal, never shown on the card
        assert "<blockquote expandable>Build an AI agent that does &lt;cool&gt; things.</blockquote>" in msg
        assert 'href="https://example.com"' in msg
        assert '<a href="https://example.com/register">Register here</a>' in msg

    def test_minimal_event(self):
        msg = format_message(make_event(), "ok")
        assert "Some Hackathon" in msg
        assert "Register here" not in msg  # no register link when none is known
        assert "🔒" not in msg
        assert "👥" not in msg
        assert "blockquote" not in msg

    def test_blank_line_after_title(self):
        msg = format_message(make_event(dates_text="Aug 1"), "ok")
        title_line, blank, rest = msg.split("\n", 2)
        assert "Some Hackathon" in title_line
        assert blank == ""

    def test_kind_picks_emoji(self):
        assert format_message(make_event(kind="hackathon"), "").startswith("🛠")
        assert format_message(make_event(kind="networking"), "").startswith("🤝")
        assert format_message(make_event(kind="program"), "").startswith("🚀")
        # unknown kinds fall back rather than crash
        assert format_message(make_event(kind="mystery"), "").startswith("🛠")

    def test_empty_reason_hides_reason_line(self):
        msg = format_message(make_event(), "")
        assert "💡" not in msg


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
        assert capsys.readouterr().out.count("would notify") == 1


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
