"""Structured (JSON lines) file logging + a shared rich console."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

console = Console()
log = logging.getLogger("savemap")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        # Extra structured fields: log.info("...", extra={"fields": {...}})
        if fields := getattr(record, "fields", None):
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(path: Path) -> None:
    if log.handlers:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
