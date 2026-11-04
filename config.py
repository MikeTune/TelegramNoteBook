"""Runtime configuration for Personal Knowledge Library.

Every value is read from the environment (or from the project's ``.env``
file).  Secrets are never stored in source code.

Local development keeps the historical behaviour: when ``DATA_DIR`` is not
set, the database, photos and exports live in the project directory.  On a
Linux VPS, set ``DATA_DIR`` to a persistent location such as
``/var/lib/knowledge-library``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load <project>/.env regardless of the current working directory.  Values
# already exported by the service manager take precedence over the file.
load_dotenv(BASE_DIR / ".env")


def _resolve_path(raw: str | None, default: Path, base: Path) -> Path:
    candidate = Path(raw).expanduser() if raw else default
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


# Persistent data root.  The default preserves the current project-local data.
DATA_DIR = _resolve_path(os.getenv("DATA_DIR"), BASE_DIR, BASE_DIR)

# Individual overrides are available for advanced deployments.
DB_PATH = str(
    _resolve_path(
        os.getenv("DB_PATH"),
        DATA_DIR / "knowledge_library.db",
        DATA_DIR,
    )
)
PHOTOS_DIR = str(
    _resolve_path(
        os.getenv("PHOTOS_DIR"),
        DATA_DIR / "document_photos",
        DATA_DIR,
    )
)
EXPORTS_DIR = str(
    _resolve_path(
        os.getenv("EXPORTS_DIR"),
        DATA_DIR / "exports",
        DATA_DIR,
    )
)

TIMEZONE_NAME = (os.getenv("TIMEZONE") or "").strip() or "Africa/Addis_Ababa"


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    """Parse ``ALLOWED_USER_IDS`` as comma/space/semicolon separated integers."""
    ids: set[int] = set()
    text = (raw or "").replace(";", ",").replace(" ", ",")
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


# Empty means "no restriction" (useful for local development).  Production
# deployments must set this to the owner's numeric Telegram user ID.
ALLOWED_USER_IDS = _parse_user_ids(os.getenv("ALLOWED_USER_IDS"))


def ensure_directories() -> None:
    """Create the persistent data directories before startup."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)
    Path(EXPORTS_DIR).mkdir(parents=True, exist_ok=True)
