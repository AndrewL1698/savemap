"""Stage 1: collect saved items from exports + urls.txt and insert them (deduped)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..config import Settings
from ..db import insert_video
from ..logs import log
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
    items, warnings = parse_urls_file(settings.resolve(settings.paths.urls_file))

    exports_dir = settings.resolve(settings.paths.exports_dir)
    if exports_dir.is_dir() and any(p.suffix == ".json" for p in exports_dir.rglob("*")):
        # TikTok/Instagram parsers get written against the real export files once they arrive.
        warnings.append(
            f"Found JSON files in {exports_dir}, but the TikTok/Instagram export parsers "
            "aren't written yet. Only urls.txt was ingested."
        )
    return items, warnings


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
