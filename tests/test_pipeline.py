import argparse

from hackathon_radar.filtering import KEYWORD_REASON_PREFIX, in_scope, keyword_score
from hackathon_radar.models import Event
from hackathon_radar.notify import format_message
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
            location="Singapore",
            prize="$10,000",
            tags=["AI", "Web"],
            register_url="https://example.com/register",
            invite_only=True,
            team_size="solo or teams up to 5",
            brief="Build an AI agent that does <cool> things.",
        )
        msg = format_message(event, 8.0, "Strong AI focus")
        assert "Hack &lt;World&gt; &amp; Co" in msg
        assert "<World>" not in msg
        assert "Aug 1 - 3, 2026" in msg
        assert "$10,000" in msg
        assert "👥 solo or teams up to 5" in msg
        assert "🔒 Invite only" in msg
        assert "Strong AI focus (8/10)" in msg
        assert "<blockquote expandable>Build an AI agent that does &lt;cool&gt; things.</blockquote>" in msg
        assert 'href="https://example.com"' in msg
        assert '<a href="https://example.com/register">Register here</a>' in msg

    def test_minimal_event(self):
        msg = format_message(make_event(), 6.0, "ok")
        assert "Some Hackathon" in msg
        assert "Register here" not in msg  # no register link when none is known
        assert "🔒" not in msg
        assert "👥" not in msg
        assert "blockquote" not in msg

    def test_empty_reason_hides_score_line(self):
        msg = format_message(make_event(), 9.0, "")
        assert "💡" not in msg
        assert "9/10" not in msg
