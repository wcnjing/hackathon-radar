import json
import sqlite3
from dataclasses import asdict
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
    payload     TEXT,
    PRIMARY KEY (source, external_id)
)
"""

META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(SCHEMA)
        self.conn.execute(META_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        # Databases created before the drip-feed queue lack the payload column.
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(events)")]
        if "payload" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN payload TEXT")

    # -- seen / recorded events ------------------------------------------------

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

    # -- notification queue ----------------------------------------------------

    def queue_event(self, event: Event, score: float, reason: str) -> None:
        """Record the event with a serialized payload so a later run can post it."""
        self.conn.execute(
            "INSERT OR IGNORE INTO events"
            " (source, external_id, title, url, first_seen, score, reason, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*event.key, event.title, event.url, _now(), score, reason,
             json.dumps(asdict(event))),
        )
        self.conn.commit()

    def pop_queued(self) -> tuple[Event, float, str] | None:
        """Best queued event (highest score, oldest first). Stays queued until
        mark_notified is called, so a failed send is retried next run."""
        row = self.conn.execute(
            "SELECT payload, score, reason FROM events"
            " WHERE notified_at IS NULL AND payload IS NOT NULL"
            " ORDER BY score DESC, first_seen ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Event(**json.loads(row[0])), row[1], row[2] or ""

    def queue_depth(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE notified_at IS NULL AND payload IS NOT NULL"
        ).fetchone()[0]

    def mark_notified(self, event: Event) -> None:
        self.conn.execute(
            "UPDATE events SET notified_at = ? WHERE source = ? AND external_id = ?",
            (_now(), *event.key),
        )
        self.conn.commit()

    # -- stats for guardrails --------------------------------------------------

    def notified_count_since(self, cutoff_iso: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE notified_at >= ?", (cutoff_iso,)
        ).fetchone()
        return row[0]

    def notified_titles_since(self, cutoff_iso: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT title FROM events WHERE notified_at >= ?", (cutoff_iso,)
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    # -- meta (small key/value state, e.g. pacing timestamps) -------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
