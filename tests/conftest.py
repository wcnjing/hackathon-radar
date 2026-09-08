import pytest


@pytest.fixture(autouse=True)
def isolate_score_cache(tmp_path, monkeypatch):
    """Keep the scoring cache out of tests unless a test opts in.

    The cache makes repeat scoring of an identical batch return the stored
    answer instead of calling the model. That is the point of it, but it would
    silently change tests that count model calls, and a test must never read or
    write the developer's real `data/score_cache.json`. So: off by default, and
    pointed at a per-test temp file for the tests that do exercise it.
    """
    from hackathon_radar import scoring

    monkeypatch.setenv("RADAR_NO_SCORE_CACHE", "1")
    # raising=False so this fixture never masks a real failure as a fixture
    # error — tests should fail on their own assertions, not on setup.
    monkeypatch.setattr(scoring, "CACHE_PATH", tmp_path / "score_cache.json", raising=False)
