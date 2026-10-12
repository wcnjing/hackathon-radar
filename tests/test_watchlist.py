from scoring_fakes import CONFIG, _Explodes, _ScoresEverything

from hackathon_radar import cli
from hackathon_radar.sources import watchlist
from hackathon_radar.sources.watchlist import PageEvent, to_event
from hackathon_radar.store import Store


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


# ---------------------------------------------------------------------------
# Outage and recovery through the real source
# ---------------------------------------------------------------------------


class TestDeferredWatchlistEventsRecover:
    """The watchlist saves a page's digest as soon as extraction succeeds, and
    scoring runs afterwards. If scoring is then degraded the event is deferred
    -- and the page, unchanged, is skipped on every later run. The retry has to
    come from the store, because this source will not return the event again.
    """

    PAGE = "https://www.nushackers.org/"

    def _config(self):
        off = {"enabled": False}
        return {
            **CONFIG,
            "sources": {
                "devpost": off,
                "mlh": off,
                "luma": off,
                "email": off,
                "watchlist": {"pages": [self.PAGE]},
            },
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }

    def _setup(self, monkeypatch, tmp_path):
        extractions = []

        class FakeWeb:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url):
                return type("R", (), {"text": "<p>AI Student Hackathon, Nov 3</p>"})()

        def extract(client, model, text, page_url):
            extractions.append(page_url)
            return [page_event(title="AI Student Hackathon", url="/h", dates_text="Nov 3")]

        monkeypatch.setattr(watchlist.httpx, "Client", lambda **kw: FakeWeb())
        monkeypatch.setattr(watchlist, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(watchlist, "_extract", extract)
        # The source's own client: extraction works throughout. Only scoring,
        # which gets its client from `cli.make_client`, has the outage.
        monkeypatch.setattr("hackathon_radar.scoring.make_client", lambda: object())
        return extractions

    def test_an_event_deferred_after_extraction_is_queued_on_recovery(self, monkeypatch, tmp_path):
        extractions = self._setup(monkeypatch, tmp_path)
        store = Store(tmp_path / "t.db")

        monkeypatch.setattr(cli, "make_client", lambda: _Explodes())
        cli._ingest(self._config(), store)
        assert store.queue_depth() == 0
        assert len(store.deferred_events()) == 1

        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        cli._ingest(self._config(), store)
        depth, left = store.queue_depth(), store.deferred_events()
        queued = store.pop_queued()[0]
        store.close()

        assert extractions == [self.PAGE], "the page was unchanged and must not be re-extracted"
        assert depth == 1, "the watchlist event stayed deferred after Claude recovered"
        assert left == []
        assert queued.source == "watchlist"
        assert queued.url == "https://www.nushackers.org/h"
        assert queued.dates_text == "Nov 3"

    def test_premise_the_source_does_not_return_it_a_second_time(self, monkeypatch, tmp_path):
        """If the watchlist ever starts re-emitting unchanged pages, the test
        above would pass without the store carrying anything."""
        self._setup(monkeypatch, tmp_path)
        cfg = self._config()["sources"]["watchlist"]
        assert len(watchlist.fetch(cfg)) == 1
        assert watchlist.fetch(cfg) == []

    def test_a_reject_on_recovery_is_final(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "make_client", lambda: _Explodes())
        cli._ingest(self._config(), store)
        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(2, "networking"))
        cli._ingest(self._config(), store)
        depth, left = store.queue_depth(), store.deferred_events()
        store.close()
        assert depth == 0 and left == []

    def test_a_scoring_client_that_cannot_be_built_also_recovers(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        store = Store(tmp_path / "t.db")

        def broken():
            raise ValueError("Unknown scheme for proxy URL")

        monkeypatch.setattr(cli, "make_client", broken)
        cli._ingest(self._config(), store)
        assert store.queue_depth() == 0 and len(store.deferred_events()) == 1

        monkeypatch.setattr(cli, "make_client", lambda: _ScoresEverything(9, "hackathon"))
        cli._ingest(self._config(), store)
        depth = store.queue_depth()
        store.close()
        assert depth == 1
