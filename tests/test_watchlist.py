from hackathon_radar.sources import watchlist
from hackathon_radar.sources.watchlist import PageEvent, to_event


class TestToEvent:
    def test_resolves_relative_url_and_assumes_country(self):
        pe = PageEvent(title="Hack&Roll 2027", url="/hacknroll", dates_text="Jan 16-17", location=None)
        ev = to_event(pe, "https://www.nushackers.org/", "SG")
        assert ev.url == "https://www.nushackers.org/hacknroll"
        assert ev.country == "SG"
        assert ev.source == "watchlist"
        assert ev.dates_text == "Jan 16-17"

    def test_event_without_link_falls_back_to_page(self):
        pe = PageEvent(title="Friday Hacks #287", url=None, dates_text=None, location="COM3, NUS")
        ev = to_event(pe, "https://www.nushackers.org/", "SG")
        assert ev.url == "https://www.nushackers.org/"

    def test_stable_id_from_page_and_title(self):
        pe = PageEvent(title="Friday Hacks  #287!", url=None, dates_text=None, location=None)
        a = to_event(pe, "https://x.org/", "SG")
        b = to_event(PageEvent(title="friday hacks #287", url=None, dates_text=None, location=None), "https://x.org/", "SG")
        assert a.external_id == b.external_id  # survives punctuation/case reposts

    def test_blank_title_dropped(self):
        assert to_event(PageEvent(title="  ", url=None, dates_text=None, location=None), "https://x.org/", "SG") is None


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
        monkeypatch.setattr(
            "hackathon_radar.scoring.make_client", lambda: object()
        )
        monkeypatch.setattr(
            watchlist,
            "_extract",
            lambda client, model, text, page_url: calls.append(page_url)
            or [PageEvent(title="Some Event", url=None, dates_text=None, location=None)],
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