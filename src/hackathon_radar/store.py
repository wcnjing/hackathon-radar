import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hackathon_radar.models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    first_seen  TEXT,
    score       REAL,
    reason      TEXT,
    notified_at TEXT,
    PRIMARY KEY (source, external_id)
)
"""


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def is_seen(self, event: Event) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM events WHERE source = ? AND external_id = ?", event.key
        ).fetchone()
        return row is not None

    def record(self, event: Event, score: float, reason: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO events (source, external_id, title, url, first_seen, score, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*event.key, event.title, event.url, _now(), score, reason),
        )
        self.conn.commit()

    def mark_notified(self, event: Event) -> None:
        self.conn.execute(
            "UPDATE events SET notified_at = ? WHERE source = ? AND external_id = ?",
            (_now(), *event.key),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
