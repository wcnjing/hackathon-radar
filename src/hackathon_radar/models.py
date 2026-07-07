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

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)
