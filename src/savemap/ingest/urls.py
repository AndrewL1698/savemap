"""Parse urls.txt: one URL per line; blank lines and #-comments ignored."""

from __future__ import annotations

from pathlib import Path

from .ids import identify
from .models import SavedItem


def parse_urls_file(path: Path) -> tuple[list[SavedItem], list[str]]:
    """Returns (items, warnings). Missing file -> no items."""
    if not path.exists():
        return [], []
    items: list[SavedItem] = []
    warnings: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ref = identify(line)
        except ValueError as e:
            warnings.append(f"{path.name}:{lineno}: {e}")
            continue
        items.append(SavedItem(ref.platform, ref.video_id, ref.url, None, "urls"))
    return items, warnings
