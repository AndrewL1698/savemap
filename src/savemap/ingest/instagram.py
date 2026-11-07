"""Parse Instagram's "Download your information" saved posts (saved/saved_posts.json).

Current format (JSON export, 2026): a top-level list of
    {"timestamp": <unix>, "media": [], "fbid": "...",
     "label_values": [{"label": "URL", "value": ..., "href": ...},
                      {"label": "Caption", ...}, {"title": "Owner", "dict": [...]}, ...]}
Older exports used {"saved_saved_media": [{"string_map_data": {"Saved on": {"href", "timestamp"}}}]};
both are accepted. Only the URL and save time are used: captions/authors come from yt-dlp.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .ids import identify
from .models import ExportFormatError, SavedItem


def _iso(ts: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _entries(data: object) -> list[tuple[str | None, object]]:
    """(url, timestamp) pairs from either known layout."""
    if isinstance(data, list) and all(isinstance(x, dict) and "label_values" in x for x in data):
        out = []
        for x in data:
            url = next((lv.get("href") or lv.get("value") for lv in x["label_values"]
                        if isinstance(lv, dict) and lv.get("label") == "URL"), None)
            out.append((url, x.get("timestamp")))
        return out
    if isinstance(data, dict) and isinstance(data.get("saved_saved_media"), list):
        out = []
        for x in data["saved_saved_media"]:
            saved = (x.get("string_map_data") or {}).get("Saved on") or {}
            out.append((saved.get("href"), saved.get("timestamp")))
        return out
    raise ExportFormatError("not an Instagram saved-posts export")


def looks_like_instagram_saved(data: object) -> bool:
    try:
        _entries(data)
        return True
    except ExportFormatError:
        return False


def parse_instagram_saved(data: object, name: str = "saved_posts.json") -> tuple[list[SavedItem], list[str]]:
    """Returns (items, warnings). Raises ExportFormatError if the structure is unknown."""
    items: list[SavedItem] = []
    warnings: list[str] = []
    for i, (url, ts) in enumerate(_entries(data)):
        if not url:
            warnings.append(f"{name}: entry {i} has no URL, skipped")
            continue
        try:
            ref = identify(url)
        except ValueError as e:
            warnings.append(f"{name}: entry {i}: {e}")
            continue
        if ref.platform != "instagram":
            warnings.append(f"{name}: entry {i} is not an Instagram link, skipped: {url}")
            continue
        items.append(SavedItem("instagram", ref.video_id, ref.url, _iso(ts), "instagram_export"))
    return items, warnings


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
