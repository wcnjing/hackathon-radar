from dataclasses import dataclass, field


@dataclass
class Event:
    source: str
    external_id: str
    title: str
    url: str
    dates_text: str = ""
    starts_at: str | None = None  # ISO 8601 when the source provides it
    ends_at: str | None = None
    location: str = ""
    country: str | None = None  # ISO 3166 alpha-2 when known
    online: bool = False
    tags: list[str] = field(default_factory=list)
    prize: str | None = None
    register_url: str | None = None  # direct signup link when distinct from url
    invite_only: bool = False
    organizer: str | None = None
    team_size: str | None = None  # e.g. "solo or teams up to 5" (enriched)
    brief: str | None = None  # 2-3 sentence challenge summary (enriched)
    deadline: str | None = None  # e.g. "register by Aug 17, 2026" (enriched)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)
