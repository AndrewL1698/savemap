from __future__ import annotations

from typing import NamedTuple


class SavedItem(NamedTuple):
    platform: str
    video_id: str
    url: str
    saved_at: str | None  # ISO-8601 UTC, when known
    source: str           # "tiktok_export" | "instagram_export" | "urls"


class ExportFormatError(Exception):
    """An export file doesn't have the structure the parser expects."""
