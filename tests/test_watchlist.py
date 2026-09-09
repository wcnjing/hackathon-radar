import pytest

from hackathon_radar.sources import watchlist
from hackathon_radar.sources.watchlist import PageEvent, to_event


def page_event(**overrides) -> PageEvent:
    defaults = dict(
        title="Some Event",
        url=None,
        dates_text=None,
        location=None,
        country_code=None,
        is_online=False,
    )
    defaults.update(overrides)
    return PageEvent(**defaults)


class TestToEvent:
    def test_resolves_relative_url_and_assumes_country(self):
        pe = page_event(title="Hack&Roll 2027", url="/hacknroll", dates_text="Jan 16-17")
        ev = to_event(pe, "https://www.nushackers.org/", "SG")
        assert ev.url == "https://www.nushackers.org/hacknroll"
        assert ev.country == "SG"
        assert ev.source == "watchlist"
        assert ev.dates_text == "Jan 16-17"

    def test_stated_country_beats_assumed(self):
        # Global pages (Jane Street etc.) list foreign events; those must not
        # be stamped SG or they'd wrongly pass the scope filter.
        pe = page_event(title="Estimathon NYC", location="New York", country_code="US")
        ev = to_event(pe, "https://www.janestreet.com/join-jane-street/events/", "SG")
        assert ev.country == "US"

    def test_online_flag_carries(self):
        ev = to_event(
            page_event(title="Virtual Trading Comp", is_online=True), "https://x.org/", "SG"
        )
        assert ev.online is True

    def test_event_without_link_falls_back_to_page(self):
        pe = page_event(title="Friday Hacks #287", location="COM3, NUS")
        ev = to_event(pe, "https://www.nushackers.org/", "SG")
        assert ev.url == "https://www.nushackers.org/"

    def test_stable_id_from_page_and_title(self):
        a = to_event(page_event(title="Friday Hacks  #287!"), "https://x.org/", "SG")
        b = to_event(page_event(title="friday hacks #287"), "https://x.org/", "SG")
        assert a.external_id == b.external_id  # survives punctuation/case reposts

    def test_blank_title_dropped(self):
        assert to_event(page_event(title="  "), "https://x.org/", "SG") is None

    @pytest.mark.parametrize(
        "raw",
        ["mailto:organizer@example.com", "javascript:alert(1)", "ftp://files.example.org/f"],
    )
    def test_unopenable_link_falls_back_to_the_page(self, raw):
        """urljoin is not a validator — it passes an absolute non-http(s)
        scheme straight through. The event keeps a real link (the page it was
        found on) rather than being dropped or carrying an unopenable one."""
        ev = to_event(page_event(title="Some Event", url=raw), "https://www.nushackers.org/", "SG")
        assert ev.url == "https://www.nushackers.org/"

    def test_schemeless_domain_falls_back_to_the_page(self):
        """urljoin would bury this under the page's own path — producing
        https://www.nushackers.org/www.foo.org/e, a 404 that still passes every
        later http(s) check."""
        ev = to_event(
            page_event(title="Some Event", url="www.foo.org/e"),
            "https://www.nushackers.org/",
            "SG",
        )
        assert ev.url == "https://www.nushackers.org/"

    @pytest.mark.parametrize("raw", ["/hacknroll", "hacknroll", "index.html", "assets/v1.2/page"])
    def test_real_relative_paths_still_resolve(self, raw):
        """The schemeless check is deliberately narrow. Relative paths legitimately
        contain dots, so a broader domain-shaped test would reject working links."""
        ev = to_event(page_event(title="E", url=raw), "https://www.nushackers.org/events/", "SG")
        assert ev.url.startswith("https://www.nushackers.org/")
        assert raw.lstrip("/") in ev.url


class TestHashGate:
    def _setup(self, monkeypatch, tmp_path, page_text):
        calls = []

        class FakeWeb:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url):
                return type("R", (), {"text": page_text})()

        monkeypatch.setattr(watchlist.httpx, "Client", lambda **kw: FakeWeb())
        monkeypatch.setattr(watchlist, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr("hackathon_radar.scoring.make_client", lambda: object())
        monkeypatch.setattr(
            watchlist,
            "_extract",
            lambda client, model, text, page_url: calls.append(page_url) or [page_event()],
        )
        return calls

    def test_unchanged_page_extracted_once(self, monkeypatch, tmp_path):
        calls = self._setup(monkeypatch, tmp_path, "<p>Friday Hacks upcoming</p>")
        cfg = {"pages": ["https://www.nushackers.org/"]}
        first = watchlist.fetch(cfg)
        second = watchlist.fetch(cfg)
        assert len(first) == 1
        assert second == []  # same content — no second Claude call
        assert len(calls) == 1

    def test_failed_extraction_retries_next_run(self, monkeypatch, tmp_path):
        calls = self._setup(monkeypatch, tmp_path, "<p>whatever</p>")

        def boom(client, model, text, page_url):
            calls.append(page_url)
            raise RuntimeError("api down")

        monkeypatch.setattr(watchlist, "_extract", boom)
        cfg = {"pages": ["https://x.org/"]}
        assert watchlist.fetch(cfg) == []
        assert watchlist.fetch(cfg) == []
        assert len(calls) == 2  # hash not saved on failure, so it retried
