from email.message import EmailMessage

from hackathon_radar.sources import email_source
from hackathon_radar.sources.email_source import body_text, new_uids, to_event
from hackathon_radar.sources.watchlist import PageEvent


def page_event(**overrides) -> PageEvent:
    defaults = dict(
        title="Some Event", url=None, dates_text=None, location=None,
        country_code=None, is_online=False,
    )
    defaults.update(overrides)
    return PageEvent(**defaults)


class TestBodyText:
    def test_prefers_plain_text(self):
        msg = EmailMessage()
        msg.set_content("AI Hackathon on Aug 5!  Register now.")
        msg.add_alternative("<p>AI Hackathon on <b>Aug 5</b>!</p>", subtype="html")
        assert body_text(msg) == "AI Hackathon on Aug 5! Register now."

    def test_falls_back_to_html_stripped(self):
        msg = EmailMessage()
        msg.add_alternative("<p>Meetup <b>tonight</b> at NTU</p>", subtype="html")
        assert body_text(msg) == "Meetup tonight at NTU"

    def test_respects_limit(self):
        msg = EmailMessage()
        msg.set_content("word " * 5_000)
        assert len(body_text(msg, limit=100)) <= 100


class TestNewUids:
    def test_filters_already_seen(self):
        # IMAP 'UID n:*' returns at least the newest message even when n > max
        assert new_uids([40, 41, 42], last_seen=41) == [42]
        assert new_uids([42], last_seen=42) == []
        assert new_uids([1, 2, 3], last_seen=0) == [1, 2, 3]


class TestToEvent:
    def test_builds_event_with_stable_id(self):
        pe = page_event(title="JPM Code for Good", url="https://jpm.example/cfg", dates_text="Sep 5")
        ev = to_event(pe, "<msg-1@mail>", "SG")
        assert ev.source == "email"
        assert ev.url == "https://jpm.example/cfg"
        assert ev.country == "SG"
        assert ev.external_id == "<msg-1@mail>#jpm code for good"

    def test_stated_country_wins(self):
        pe = page_event(title="NYC Datathon", url="https://x.example", country_code="US")
        assert to_event(pe, "<m@x>", "SG").country == "US"

    def test_linkless_event_skipped(self):
        assert to_event(page_event(title="Vague Event", url=None), "<m@x>", "SG") is None


class TestFetchGuards:
    def test_missing_credentials_skips_quietly(self, monkeypatch):
        monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
        monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
        assert email_source.fetch({}) == []

    def test_password_spaces_stripped(self, monkeypatch, tmp_path):
        """Google displays app passwords with spaces; login must strip them."""
        seen = {}

        class FakeImap:
            def __init__(self, host):
                pass

            def login(self, user, password):
                seen["password"] = password
                raise email_source.imaplib.IMAP4.error("stop here")

        monkeypatch.setenv("EMAIL_ADDRESS", "feed@example.com")
        monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
        monkeypatch.setattr(email_source.imaplib, "IMAP4_SSL", FakeImap)
        monkeypatch.setattr("hackathon_radar.scoring.make_client", lambda: object())
        monkeypatch.setattr(email_source, "STATE_PATH", tmp_path / "state.json")

        assert email_source.fetch({}) == []
        assert seen["password"] == "abcdefghijklmnop"
