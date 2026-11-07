"""Stage 1: collect saved items from exports + urls.txt and insert them (deduped)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..config import Settings
from ..db import insert_video
from ..logs import log
from .instagram import load_json, looks_like_instagram_saved, parse_instagram_saved
from .models import ExportFormatError, SavedItem
from .urls import parse_urls_file

__all__ = ["ExportFormatError", "IngestResult", "SavedItem", "collect", "run_ingest"]


@dataclass
class IngestResult:
    found: int = 0
    inserted: int = 0
    duplicates: int = 0
    warnings: list[str] = field(default_factory=list)


def collect(settings: Settings) -> tuple[list[SavedItem], list[str]]:
    """Exports first (they carry save dates), then urls.txt."""
    items: list[SavedItem] = []
    warnings: list[str] = []

    exports_dir = settings.resolve(settings.paths.exports_dir)
    for path in sorted(exports_dir.rglob("*.json")) if exports_dir.is_dir() else []:
        try:
            data = load_json(path)
        except (OSError, ValueError) as e:
            warnings.append(f"{path.name}: can't read JSON ({e})")
            continue
        if looks_like_instagram_saved(data):
            found, warns = parse_instagram_saved(data, path.name)
            items += found
            warnings += warns
        else:
            # The TikTok parser gets written against the real export file once it arrives.
            warnings.append(f"{path.name}: unrecognized export format, skipped")

    found, warns = parse_urls_file(settings.resolve(settings.paths.urls_file))
    return items + found, warnings + warns


def run_ingest(conn: sqlite3.Connection, settings: Settings, limit: int | None = None) -> IngestResult:
    items, warnings = collect(settings)
    res = IngestResult(found=len(items), warnings=warnings)
    for item in items:
        if limit is not None and res.inserted >= limit:
            break
        if insert_video(conn, **item._asdict()):
            res.inserted += 1
            log.info("ingested", extra={"fields": {"platform": item.platform, "video_id": item.video_id}})
        else:
            res.duplicates += 1
    conn.commit()
    for w in warnings:
        log.warning(w)
    return res
