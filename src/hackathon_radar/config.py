import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict:
    """Load config.toml and .env. RADAR_CONFIG env var overrides the path."""
    config_path = path or Path(os.environ.get("RADAR_CONFIG", PROJECT_ROOT / "config.toml"))
    load_dotenv(config_path.parent / ".env")
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def db_path() -> Path:
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "radar.db"
