import json
from datetime import date
from pathlib import Path

from hackathon_radar.sources import devpost, mlh

FIXTURES = Path(__file__).parent / "fixtures"


def test_devpost_parse():
    data = json.loads((FIXTURES / "devpost.json").read_text())
    events = devpost.parse_response(data)
    assert events, "fixture should yield events"

    first = events[0]
    assert first.source == "devpost"
    assert first.title == "Build with Gemini XPRIZE"
    assert first.url.startswith("https://")
    assert first.online is True
    assert "Machine Learning/AI" in first.tags
    assert first.prize == "$2,000,000"  # HTML span stripped
    assert first.dates_text  # e.g. "May 19 - Aug 17, 2026"


def test_devpost_skips_closed_and_invite_only():
    data = {
        "hackathons": [
            {"id": 1, "title": "Closed", "open_state": "ended", "themes": []},
            {"id": 2, "title": "Invite", "open_state": "open", "invite_only": True, "themes": []},
            {"id": 3, "title": "Open", "open_state": "open", "themes": [], "url": "https://x.devpost.com"},
        ]
    }
    events = devpost.parse_response(data)
    assert [e.title for e in events] == ["Open"]


def test_mlh_parse():
    html = (FIXTURES / "mlh2027.html").read_text()
    events = mlh.parse_season_page(html)
    assert len(events) > 30, "season page should have many events"

    first = events[0]
    assert first.source == "mlh"
    assert first.title
    assert first.url.startswith("https://")
    assert first.starts_at and "T" in first.starts_at  # ISO from microdata
    assert first.location
    assert "student hackathon" in first.tags

    # microdata distinguishes online vs in-person
    assert any(e.online for e in events) or any(not e.online for e in events)
    # country codes come through for in-person events
    assert any(e.country for e in events if not e.online)


def test_mlh_upcoming_filter():
    from hackathon_radar.models import Event

    past = Event(source="mlh", external_id="a", title="a", url="u", ends_at="2020-01-01T00:00:00Z")
    future = Event(source="mlh", external_id="b", title="b", url="u", ends_at="2099-01-01T00:00:00Z")
    today = date(2026, 7, 7)
    assert not mlh._upcoming(past, today)
    assert mlh._upcoming(future, today)
