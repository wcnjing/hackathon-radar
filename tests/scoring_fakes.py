"""Fake Anthropic clients and event builders shared by the scoring tests."""

import json

import httpx

from hackathon_radar.models import Event
from hackathon_radar.scoring import ScoreBatch, ScoredEvent

INTERESTS = {
    "keywords": ["ai", "meetup", "networking", "hackathon", "student"],
    "min_score": 6,
    "min_score_by_kind": {"networking": 8},
}
CONFIG = {"interests": INTERESTS, "scoring": {"batch_size": 20}, "notify": {}}


def make_event(title="Some Hackathon", source="test", external_id="1", **kw) -> Event:
    return Event(
        source=source, external_id=external_id, title=title, url="https://example.com", **kw
    )


class _Explodes:
    """Stands in for a transient 529."""

    class messages:
        @staticmethod
        def parse(*a, **k):
            raise RuntimeError("transient 529 overloaded")


class _AuthFails:
    class messages:
        @staticmethod
        def parse(*a, **k):
            import anthropic

            raise anthropic.AuthenticationError(
                "bad key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None,
            )


class _ScoresEverything:
    """A healthy client: returns one score per event, all of them low."""

    def __init__(self, score=2, kind="networking"):
        self.score, self.kind = score, kind
        self.calls = 0
        self.messages = self

    def parse(self, **kw):
        self.calls += 1
        ids = [e["id"] for e in json.loads(kw["messages"][0]["content"].split("\n", 1)[1])]
        batch = ScoreBatch(
            scores=[
                ScoredEvent(id=i, score=self.score, reason="r", kind=self.kind, level="unclear")
                for i in ids
            ]
        )
        return type("R", (), {"parsed_output": batch})()
