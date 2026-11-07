"""Stage 5: write the Obsidian vault (Videos/, Topics/, Areas/, Index.md, Review/).

Re-running is safe: everything from the `<!-- my notes -->` marker down is preserved, and so is
a `reviewed:` value you changed in a video note's frontmatter. Files are only rewritten when
their content changes. Notes are never deleted.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db
from .config import ConfigError, Settings
from .enrich import Enrichment, clean_llm_text
from .llm import LLM, ProviderChain
from .logs import console, log
from .ratelimit import StopStage
from .taxonomy import Taxonomy

MARKER = "<!-- my notes -->"
REVIEW_COUNT = 5
_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_REVIEWED = re.compile(r"^reviewed:\s*(true|false)\s*$", re.MULTILINE | re.IGNORECASE)
PLATFORM_NAMES = {"tiktok": "TikTok", "instagram": "Instagram"}


# ---------- small helpers ----------

def safe_name(text: str, max_len: int = 90) -> str:
    """A filename Obsidian can link to (no / \\ : * ? " < > | # ^ [ ])."""
    s = " ".join(_BAD_CHARS.sub("-", text).split()).strip(" .-")
    return s[:max_len].rstrip(" .-") or "untitled"


def short_id(video_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", video_id)[-6:] or "x"


def link(folder: str, name: str, label: str | None = None) -> str:
    """Path-qualified wikilink, so an area and a topic with the same name never collide."""
    return f"[[{folder}/{name}|{label or name}]]"


def yaml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # JSON strings are valid YAML double-quoted scalars


def yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(yaml_str(i) for i in items) + "]"


def obsidian_tag(tag: str) -> str:
    t = re.sub(r"[^\w/-]+", "-", tag.strip()).strip("-")
    return t if t and not t.isdigit() else f"t-{t}"


def merge(path: Path, generated: str) -> tuple[str, bool | None]:
    """Combine freshly generated text with the user's part of the existing note.

    Returns (final text, reviewed value found in the existing note or None).
    """
    tail, reviewed = f"{MARKER}\n", None
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if (i := old.find(MARKER)) != -1:
            tail = old[i:]
        if old.startswith("---") and (m := _REVIEWED.search(old.split("\n---", 1)[0])):
            reviewed = m.group(1).lower() == "true"
    return f"{generated.rstrip()}\n\n{tail}", reviewed


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


# ---------- data ----------

@dataclass
class VideoNote:
    row: sqlite3.Row
    e: Enrichment
    name: str                        # file name without .md (stable, stored as slug)
    topics: list[tuple[str, str]]    # canonical (area, topic)
    reviewed: bool = False

    @property
    def saved(self) -> str:
        return (self.row["saved_at"] or self.row["created_at"] or "")[:10]


def assign_names(conn: sqlite3.Connection, rows: list[sqlite3.Row], enrichments: dict[int, Enrichment]) -> dict[int, str]:
    """Reuse stored slugs; give new videos '<Title> (<short id>)', unique across the vault."""
    taken = {r["slug"] for r in conn.execute("SELECT slug FROM videos WHERE slug IS NOT NULL")}
    names: dict[int, str] = {}
    for r in rows:
        if r["slug"]:
            names[r["id"]] = r["slug"]
            continue
        base, sid = safe_name(enrichments[r["id"]].title), short_id(r["video_id"])
        name, n = f"{base} ({sid})", 2
        while name in taken:
            name, n = f"{base} ({sid}-{n})", n + 1
        taken.add(name)
        db.update(conn, r["id"], slug=name)
        names[r["id"]] = name
    return names


# ---------- renderers ----------

def render_video(v: VideoNote) -> str:
    r, e = v.row, v.e
    areas = list(dict.fromkeys(a for a, _ in v.topics))
    platform = PLATFORM_NAMES.get(r["platform"], r["platform"])
    fm = [
        "---",
        f"title: {yaml_str(e.title)}",
        f"url: {r['url']}",
        f"platform: {r['platform']}",
        f"author: {yaml_str(r['author'] or '')}",
        f"saved: {v.saved}",
        f"content_type: {e.content_type}",
        f"areas: {yaml_list(areas)}",
        f"topics: {yaml_list([t for _, t in v.topics])}",
        f"tags: {yaml_list([obsidian_tag(t) for t in e.tags])}",
        f"actionable: {str(e.actionable).lower()}",
        f"reviewed: {str(v.reviewed).lower()}",
        "---",
    ]
    body = [f"# {e.title}", "", e.summary, ""]
    if e.key_takeaways:
        body += ["## Key takeaways", *[f"- {t}" for t in e.key_takeaways], ""]
    if e.actionable and e.action:
        body += ["## Try this", f"- [ ] {e.action}", ""]
    if e.resources:
        body += ["## Resources"]
        for res in e.resources:
            name = f"[{res.name}]({res.url})" if res.url else res.name
            body.append(f"- {name} ({res.kind})")
        body.append("")
    body += [
        "## Links",
        "- Topics: " + " · ".join(link("Topics", safe_name(t), t) for _, t in v.topics),
        f"- Original: [Watch on {platform}]({r['url']})" + (f" by {r['author']}" if r["author"] else ""),
    ]
    if r["needs_visual"]:
        body.append("- Note: mostly visual video (little speech); the summary is based on the caption.")
    return "\n".join(fm + [""] + body) + "\n"


def render_topic(area: str, topic: str, videos: list[VideoNote], overview: str | None) -> str:
    lines = [
        "---", f"area: {yaml_str(area)}", "type: topic", f"videos: {len(videos)}", "---", "",
        f"# {topic}", "", f"Area: {link('Areas', safe_name(area), area)}", "",
    ]
    if overview:
        lines += [overview, ""]
    lines += [f"## Videos ({len(videos)})"]
    for v in sorted(videos, key=lambda v: v.saved, reverse=True):
        flag = " ✅" if v.reviewed else ""
        lines.append(f"- {link('Videos', v.name, v.e.title)} · {v.e.content_type}{flag}")
    return "\n".join(lines) + "\n"


def render_area(area: str, topics: dict[str, list[VideoNote]]) -> str:
    total = len({v.row["id"] for vs in topics.values() for v in vs})
    lines = ["---", "type: area", f"videos: {total}", "---", "", f"# {area}", "",
             f"[[Index|← Index]] · {total} videos", "", "## Topics"]
    for topic, vs in sorted(topics.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"- {link('Topics', safe_name(topic), topic)} ({len(vs)})")
    return "\n".join(lines) + "\n"


def render_index(tree: dict[str, dict[str, list[VideoNote]]], n_videos: int, n_unavailable: int) -> str:
    lines = ["# savemap index", "",
             f"{n_videos} videos across {len(tree)} areas. [[Review/This week|This week's review →]]", ""]
    if n_unavailable:
        lines += [f"_{n_unavailable} saved videos were deleted or private and are not included._", ""]
    for area, topics in sorted(tree.items(), key=lambda kv: -sum(len(v) for v in kv[1].values())):
        count = len({v.row["id"] for vs in topics.values() for v in vs})
        lines.append(f"## {link('Areas', safe_name(area), area)} ({count})")
        for topic, vs in sorted(topics.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"- {link('Topics', safe_name(topic), topic)} ({len(vs)})")
        lines.append("")
    return "\n".join(lines)


def pick_review(videos: list[VideoNote], today: date) -> list[VideoNote]:
    """REVIEW_COUNT random unreviewed actionable videos; stable for the whole ISO week."""
    year, week, _ = today.isocalendar()
    pool = sorted((v for v in videos if v.e.actionable and not v.reviewed), key=lambda v: v.name)
    return random.Random(f"{year}-W{week:02d}").sample(pool, min(REVIEW_COUNT, len(pool)))


def render_review(picks: list[VideoNote], today: date) -> str:
    year, week, _ = today.isocalendar()
    lines = [f"# This week ({year}-W{week:02d})", "",
             "Five saved videos with something to act on. Set `reviewed: true` in a video's note "
             "when you're done; it won't be picked again.", ""]
    for v in picks:
        lines += [f"## {link('Videos', v.name, v.e.title)}", f"- [ ] {v.e.action or v.e.summary}", ""]
    if not picks:
        lines.append("Nothing left to review 🎉")
    return "\n".join(lines)


# ---------- topic overviews ----------

OVERVIEW_SYSTEM = ("You write a one-paragraph overview (3-5 sentences) of what someone has saved on a topic, "
                   "based on the video titles and summaries given. Mention the recurring themes and the most "
                   "useful ideas. Address the reader as 'you'. Respond with JSON only.")
OVERVIEW_SCHEMA = {"type": "object", "properties": {"overview": {"type": "string"}},
                   "required": ["overview"], "additionalProperties": False}


class Overviews:
    """Cached per topic; regenerated only when the topic's video count changes."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, enabled: bool, llm: LLM | None) -> None:
        self.conn, self.settings, self.enabled, self._llm = conn, settings, enabled, llm
        self.generated, self.unavailable = 0, None

    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = ProviderChain.from_settings(self.settings)
        return self._llm

    def get(self, topic: str, videos: list[VideoNote]) -> str | None:
        row = self.conn.execute("SELECT video_count, text FROM topic_overviews WHERE topic = ?", (topic,)).fetchone()
        if row and row["video_count"] == len(videos):
            return row["text"]
        if not self.enabled or self.unavailable:
            return row["text"] if row else None
        items = "\n".join(f"- {v.e.title}: {v.e.summary}" for v in videos[:40])
        try:
            raw = self.llm().complete_json(OVERVIEW_SYSTEM, f"Topic: {topic}\nVideos:\n{items}",
                                           OVERVIEW_SCHEMA, "topic_overview")
            text = clean_llm_text(str(json.loads(raw)["overview"]))
        except (StopStage, ConfigError) as e:
            self.unavailable = str(e)
            return row["text"] if row else None
        except Exception as e:  # one bad overview shouldn't block the export
            log.error("overview failed", extra={"fields": {"topic": topic, "error": str(e)[:500]}})
            return row["text"] if row else None
        self.conn.execute("INSERT OR REPLACE INTO topic_overviews (topic, video_count, text) VALUES (?, ?, ?)",
                          (topic, len(videos), text))
        self.conn.commit()
        self.generated += 1
        return text


# ---------- stage ----------

@dataclass
class ExportResult:
    videos: int = 0
    topics: int = 0
    areas: int = 0
    written: int = 0
    unchanged: int = 0
    overviews_generated: int = 0
    overview_problem: str | None = None


def run_export(conn: sqlite3.Connection, settings: Settings, limit: int | None = None, *,
               overviews: bool = True, llm: LLM | None = None, today: date | None = None) -> ExportResult:
    today = today or date.today()
    vault = settings.resolve(settings.paths.vault_dir)
    sql = "SELECT * FROM videos WHERE state = 'enriched' ORDER BY saved_at IS NULL, saved_at, id"
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
    res = ExportResult()
    enrichments = {r["id"]: Enrichment.model_validate_json(r["enrichment"]) for r in rows}
    names = assign_names(conn, rows, enrichments)

    # Re-canonicalize at export time so alias edits in config.toml take effect without re-enriching.
    taxonomy = Taxonomy(settings.taxonomy)
    notes: list[VideoNote] = []
    for r in rows:
        e = enrichments[r["id"]]
        pairs = list(dict.fromkeys(taxonomy.canonical(t.area, t.topic) for t in e.topics))
        notes.append(VideoNote(r, e, names[r["id"]], pairs))

    def put(path: Path, generated: str) -> bool | None:
        text, reviewed = merge(path, generated)
        if write_if_changed(path, text):
            res.written += 1
        else:
            res.unchanged += 1
        return reviewed

    # Video notes: read back a `reviewed:` the user set, then write with it preserved.
    for v in notes:
        path = vault / "Videos" / f"{v.name}.md"
        _, v.reviewed = merge(path, "")
        v.reviewed = bool(v.reviewed)
        put(path, render_video(v))
    res.videos = len(notes)

    tree: dict[str, dict[str, list[VideoNote]]] = defaultdict(lambda: defaultdict(list))
    for v in notes:
        for area, topic in v.topics:
            tree[area][topic].append(v)

    ov = Overviews(conn, settings, overviews, llm)
    with console.status("") as status:
        for area, topics in tree.items():
            for topic, vs in topics.items():
                status.update(f"export: topic {topic}")
                put(vault / "Topics" / f"{safe_name(topic)}.md", render_topic(area, topic, vs, ov.get(topic, vs)))
                res.topics += 1
            put(vault / "Areas" / f"{safe_name(area)}.md", render_area(area, topics))
            res.areas += 1

    n_unavailable = conn.execute("SELECT COUNT(*) FROM videos WHERE state = 'unavailable'").fetchone()[0]
    put(vault / "Index.md", render_index(tree, len(notes), n_unavailable))
    put(vault / "Review" / "This week.md", render_review(pick_review(notes, today), today))
    res.overviews_generated, res.overview_problem = ov.generated, ov.unavailable
    log.info("exported", extra={"fields": {"videos": res.videos, "written": res.written}})
    return res
