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
    # raising=True (the default) on purpose. With raising=False, renaming
    # CACHE_PATH would make this line quietly create a dead attribute and every
    # test would then read and write the developer's real data/score_cache.json.
    # For a fixture whose entire job is isolation, failing loudly on a rename is
    # the safe behaviour -- the noisy error is the feature.
    monkeypatch.setattr(scoring, "CACHE_PATH", tmp_path / "score_cache.json")
