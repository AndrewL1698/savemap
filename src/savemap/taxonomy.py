"""Open-ended area -> topic hierarchy.

Seeds come from config; topics the LLM creates during enrich are added as they appear.
Names are matched loosely (case, spacing, "&" vs "and") and passed through config
aliases, so near-identical names collapse into one. Each topic belongs to exactly one
area: the first area it was seen under wins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import Taxonomy as TaxonomyConfig


def name_key(name: str) -> str:
    s = name.casefold().replace("&", " and ")
    s = re.sub(r"[^\w/+#.]+", " ", s)  # keep c++, c#, ai/ml, node.js
    return " ".join(s.split())


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


class Taxonomy:
    def __init__(self, cfg: TaxonomyConfig, seen: Iterable[tuple[str, str]] = ()) -> None:
        self._aliases = {name_key(k): clean_name(v) for k, v in cfg.aliases.items()}
        self._areas: dict[str, str] = {}               # key -> display name
        self._topics: dict[str, tuple[str, str]] = {}  # topic key -> (area, topic)
        for area, topics in cfg.areas.items():
            a = self.add_area(area)
            for t in topics:
                self.add(a, t)
        for area, topic in seen:
            self.add(area, topic)

    def _alias(self, name: str) -> str:
        name = clean_name(name)
        return self._aliases.get(name_key(name), name)

    def add_area(self, area: str) -> str:
        area = self._alias(area)
        return self._areas.setdefault(name_key(area), area)

    def add(self, area: str, topic: str) -> tuple[str, str]:
        """Register (area, topic) if the topic is new; return the canonical (area, topic).

        A topic already known under another area keeps its original area.
        """
        topic = self._alias(topic)
        key = name_key(topic)
        if key not in self._topics:
            self._topics[key] = (self.add_area(area), topic)
        return self._topics[key]

    canonical = add

    @property
    def areas(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {a: [] for a in self._areas.values()}
        for area, topic in self._topics.values():
            out[area].append(topic)
        return out

    def prompt_listing(self) -> str:
        """Compact listing for the enrich prompt."""
        return "\n".join(
            f"- {area}: {', '.join(topics) if topics else '(no topics yet)'}"
            for area, topics in self.areas.items()
        )
