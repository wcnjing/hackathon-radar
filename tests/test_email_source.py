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
    def test_prefers_plain_text_when_it_has_links(self):
        msg = EmailMessage()
        msg.set_content("AI Hackathon Aug 5! Sign up: https://x.example/reg")
        msg.add_alternative("<p>AI Hackathon <b>Aug 5</b>!</p>", subtype="html")
        assert body_text(msg) == "AI Hackathon Aug 5! Sign up: https://x.example/reg"

    def test_linkfree_plain_defers_to_html_with_links(self):
        """Regression: Unstop-style emails carry URLs only in the HTML part;
        preferring the link-free plain part made every event non-actionable."""
        msg = EmailMessage()
        msg.set_content("Tech-A-Thon 5.0 is live! See email for details.")
        msg.add_alternative(
            '<p>Tech-A-Thon 5.0 is live! <a href="https://unstop.com/t5">Register</a></p>',
            subtype="html",
        )
        assert "Register (https://unstop.com/t5)" in body_text(msg)

    def test_empty_plain_falls_back_to_html(self):
        msg = EmailMessage()
        msg.set_content("")
        msg.add_alternative("<p>Meetup <b>tonight</b> at NTU</p>", subtype="html")
        assert body_text(msg) == "Meetup tonight at NTU"

    def test_falls_back_to_html_stripped(self):
        msg = EmailMessage()
        msg.add_alternative("<p>Meetup <b>tonight</b> at NTU</p>", subtype="html")
        assert body_text(msg) == "Meetup tonight at NTU"

    def test_respects_limit(self):
        msg = EmailMessage()
        msg.set_content("word " * 5_000)
        assert len(body_text(msg, limit=100)) <= 100


class TestAttachments:
    def _msg_with(self, **attachments) -> EmailMessage:
        msg = EmailMessage()
        msg.set_content("See attached for details: https://x.example")
        for name, (data, maintype, subtype) in attachments.items():
            if isinstance(data, str):  # text/* content: maintype is implied
                msg.add_attachment(data, subtype=subtype, filename=name)
            else:
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        return msg

    def test_ics_attachment_flattened_to_text(self):
        ics = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:NTU AI Night\r\nDTSTART;TZID=Asia/Singapore:20260801T183000\r\n"
            "LOCATION:LT2A\r\nURL:https://x.example/ai-night\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        msg = self._msg_with(**{"invite.ics": (ics, "text", "calendar")})
        text, images = email_source.attachment_content(msg)
        assert "Summary: NTU AI Night" in text
        assert "Url: https://x.example/ai-night" in text
        assert images == []

    def test_image_becomes_vision_block_and_oversize_skipped(self):
        small = b"\x89PNG small poster bytes"
        huge = b"x" * (email_source.MAX_ATTACHMENT_BYTES + 1)
        msg = self._msg_with(
            **{"poster.png": (small, "image", "png"), "huge.png": (huge, "image", "png")}
        )
        text, images = email_source.attachment_content(msg)
        assert len(images) == 1
        assert images[0]["source"]["media_type"] == "image/png"
        import base64

        assert base64.b64decode(images[0]["source"]["data"]) == small

    def test_image_cap(self):
        parts = {f"p{i}.png": (b"img", "image", "png") for i in range(4)}
        _, images = email_source.attachment_content(self._msg_with(**parts))
        assert len(images) == email_source.MAX_IMAGES

    def test_garbage_pdf_ignored(self):
        msg = self._msg_with(**{"broken.pdf": (b"not a pdf", "application", "pdf")})
        text, images = email_source.attachment_content(msg)
        assert text == "" and images == []


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
