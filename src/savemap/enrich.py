"""Stage 4: one LLM call per video -> validated title/summary/topics/tags/... JSON."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

from . import db
from .config import Settings
from .llm import LLM, ProviderChain
from .logs import console, log
from .ratelimit import StopStage
from .taxonomy import Taxonomy

MAX_TAKEAWAYS, MAX_TOPICS, MAX_TAGS = 5, 3, 6
_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u00a0": " "})
RESOURCE_KINDS = ["repo", "library", "tool", "website", "book", "course", "video", "other"]


class Resource(BaseModel):
    name: str = Field(min_length=1)
    kind: str = "other"
    url: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: object) -> str:
        v = str(v or "other").lower().strip()
        return v if v in RESOURCE_KINDS else "other"

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v: object) -> str | None:
        return str(v).strip() or None if v else None


class TopicRef(BaseModel):
    area: str = Field(min_length=1)
    topic: str = Field(min_length=1)


class Enrichment(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=10)
    key_takeaways: list[str]
    content_type: str
    topics: list[TopicRef] = Field(min_length=1)
    tags: list[str]
    resources: list[Resource]
    actionable: bool
    action: str | None = None

    @field_validator("title", "summary", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return " ".join(v.translate(_DASHES).split()) if isinstance(v, str) else v

    @field_validator("key_takeaways", mode="after")
    @classmethod
    def _takeaways(cls, v: list[str]) -> list[str]:
        return [t.strip() for t in v if t.strip()][:MAX_TAKEAWAYS]

    @field_validator("topics", mode="after")
    @classmethod
    def _topics(cls, v: list[TopicRef]) -> list[TopicRef]:
        return v[:MAX_TOPICS]

    @field_validator("tags", mode="after")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        cleaned = (" ".join(t.lower().lstrip("#").split()) for t in v)
        return list(dict.fromkeys(t for t in cleaned if t))[:MAX_TAGS]

    @field_validator("content_type", mode="after")
    @classmethod
    def _content_type(cls, v: str, info: ValidationInfo) -> str:
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        allowed = (info.context or {}).get("content_types")
        if allowed and v not in allowed:
            raise ValueError(f"content_type must be one of {allowed}, got {v!r}")
        return v

    @field_validator("action", mode="after")
    @classmethod
    def _action(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


def parse_enrichment(raw: str, content_types: list[str]) -> Enrichment:
    """Raises ValidationError (incl. for invalid JSON)."""
    return Enrichment.model_validate_json(raw, context={"content_types": content_types})


def canonicalize(e: Enrichment, taxonomy: Taxonomy) -> Enrichment:
    """Map topics onto existing names (registering new ones) and drop resulting duplicates."""
    seen: dict[tuple[str, str], TopicRef] = {}
    for t in e.topics:
        area, topic = taxonomy.canonical(t.area, t.topic)
        seen.setdefault((area, topic), TopicRef(area=area, topic=topic))
    return e.model_copy(update={"topics": list(seen.values())})


def llm_schema(content_types: list[str]) -> dict:
    """Strict-mode JSON schema (every field required, no extra keys). Limits enforced by pydantic."""
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    str_list = {"type": "array", "items": {"type": "string"}}
    return obj({
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_takeaways": str_list,
        "content_type": {"type": "string", "enum": content_types},
        "topics": {"type": "array", "items": obj({"area": {"type": "string"}, "topic": {"type": "string"}})},
        "tags": str_list,
        "resources": {"type": "array", "items": obj({
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": RESOURCE_KINDS},
            "url": {"type": ["string", "null"]},
        })},
        "actionable": {"type": "boolean"},
        "action": {"type": ["string", "null"]},
    })


SYSTEM = f"""You turn a saved short-form video (TikTok / Instagram Reel) into a structured note \
for the viewer's personal knowledge base. Base everything on the transcript and caption; never \
invent facts that aren't there. Respond with JSON only.

Fields:
- title: short, specific, descriptive (max ~70 chars). Say what it's about, not the creator's clickbait.
- summary: 2-3 plain sentences: what the video says or shows.
- key_takeaways: up to {MAX_TAKEAWAYS} concrete points worth remembering (empty list if none).
- content_type: the single best fit from the allowed values.
- topics: 1-{MAX_TOPICS} {{area, topic}} pairs, most relevant first.
  * REUSE an existing area/topic from the list below whenever one reasonably fits. Copy its exact name.
  * Only create a new topic if none fit. Name it as a broad, reusable category that future videos \
could share (e.g. "Roblox", "Internships", "YouTube growth"), not a description of this one video. \
Put it under the best existing area; create a new area only for a genuinely different life domain.
- tags: up to {MAX_TAGS} lowercase keywords (tools, languages, concepts), no '#'.
- resources: repos, libraries, tools, sites, books or courses explicitly mentioned (name, kind, url \
if stated, else null). Empty list if none.
- actionable: true if there's something concrete the viewer could go do.
- action: one line describing that thing to do (null if not actionable)."""


def build_user_prompt(row: sqlite3.Row, taxonomy: Taxonomy, transcript_chars: int) -> str:
    tags = " ".join(f"#{t}" for t in json.loads(row["hashtags"] or "[]"))
    transcript = (row["transcript"] or "").strip()
    if len(transcript) > transcript_chars:
        transcript = transcript[:transcript_chars] + " …[truncated]"
    parts = [
        "Existing areas and topics:",
        taxonomy.prompt_listing(),
        "",
        f"Platform: {row['platform']}",
        f"Creator: {row['author'] or 'unknown'}",
        f"Caption: {row['caption'] or '(none)'}",
        f"Hashtags: {tags or '(none)'}",
        f"Transcript: {transcript or '(no speech)'}",
    ]
    if row["needs_visual"]:
        parts.append("Note: little or no speech; the video is probably mostly visual (on-screen text/code). "
                     "Rely on the caption and be conservative.")
    return "\n".join(parts)


def load_taxonomy(conn: sqlite3.Connection, settings: Settings) -> Taxonomy:
    seen: list[tuple[str, str]] = []
    for (raw,) in conn.execute("SELECT enrichment FROM videos WHERE enrichment IS NOT NULL ORDER BY id"):
        seen += [(t["area"], t["topic"]) for t in json.loads(raw).get("topics", [])]
    return Taxonomy(settings.taxonomy, seen)


def enrich_one(llm: LLM, row: sqlite3.Row, taxonomy: Taxonomy, settings: Settings) -> Enrichment:
    """Call the LLM, validate, retry once with the validation error. Raises ValidationError."""
    types = settings.taxonomy.content_types
    schema = llm_schema(types)
    user = build_user_prompt(row, taxonomy, settings.llm.transcript_chars)
    raw = llm.complete_json(SYSTEM, user, schema, "video_note")
    try:
        result = parse_enrichment(raw, types)
    except ValidationError as e:
        log.info("invalid enrichment, retrying once", extra={"fields": {"id": row["id"], "error": str(e)[:500]}})
        retry = (f"{user}\n\nYour previous answer was invalid:\n{e}\n"
                 f"Previous answer:\n{raw[:3000]}\n\nReturn corrected JSON only.")
        result = parse_enrichment(llm.complete_json(SYSTEM, retry, schema, "video_note"), types)
    return canonicalize(result, taxonomy)


@dataclass
class EnrichResult:
    done: int = 0
    failed: int = 0
    new_topics: int = 0
    stopped: str | None = None


def run_enrich(conn: sqlite3.Connection, settings: Settings, limit: int | None = None,
               llm: LLM | None = None) -> EnrichResult:
    rows = db.pending(conn, "transcribed", settings.pipeline.max_attempts, limit)
    res = EnrichResult()
    if not rows:
        return res
    llm = llm or ProviderChain.from_settings(settings)
    taxonomy = load_taxonomy(conn, settings)
    topics_before = sum(len(t) for t in taxonomy.areas.values())
    with console.status("") as status:
        for i, row in enumerate(rows):
            status.update(f"enrich {i + 1}/{len(rows)}: {row['url']}")
            fields = {"id": row["id"], "url": row["url"]}
            try:
                e = enrich_one(llm, row, taxonomy, settings)
            except StopStage as exc:
                res.stopped = str(exc)
                log.warning("enrich stopped", extra={"fields": fields | {"reason": str(exc)}})
                break
            except Exception as exc:
                db.fail(conn, row["id"], "enrich", f"{type(exc).__name__}: {exc}")
                res.failed += 1
                log.error("enrich failed", extra={"fields": fields | {"error": str(exc)[:1000]}})
                continue
            db.advance(conn, row["id"], "enriched", enrichment=e.model_dump_json())
            res.done += 1
            log.info("enriched", extra={"fields": fields | {
                "provider": getattr(llm, "name", "?"), "title": e.title,
                "topics": [f"{t.area} / {t.topic}" for t in e.topics]}})
    res.new_topics = sum(len(t) for t in taxonomy.areas.values()) - topics_before
    return res
