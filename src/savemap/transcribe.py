"""Stage 3: speech-to-text with Groq Whisper, filtering out non-speech hallucinations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import Settings
from .logs import console, log
from .ratelimit import StopStage, call_with_backoff

PROMPT_CHARS = 600  # Groq allows a 224-token prompt; stay well under


@dataclass
class TranscribeResult:
    done: int = 0
    needs_visual: int = 0
    failed: int = 0
    redownload: int = 0
    stopped: str | None = None


def build_prompt(caption: str | None, hashtags_json: str | None) -> str:
    """Context for Whisper: helps with names/jargon (e.g. 'Roblox', 'LeetCode')."""
    tags = json.loads(hashtags_json or "[]")
    text = " ".join(filter(None, [caption or "", " ".join(f"#{t}" for t in tags)]))
    return text[:PROMPT_CHARS]


def speech_text(segments: list[dict], no_speech_threshold: float) -> str:
    """Join segments that are likely real speech; drops Whisper's music/silence hallucinations."""
    kept = [s["text"].strip() for s in segments
            if s.get("no_speech_prob", 0) <= no_speech_threshold and s.get("text", "").strip()]
    return " ".join(kept)


def word_count(text: str) -> int:
    return len(text.split())


class GroqTranscriber:
    def __init__(self, settings: Settings) -> None:
        from groq import Groq  # imported lazily so other stages don't need the key

        self.model = settings.transcribe.model
        self.client = Groq(api_key=settings.secret("GROQ_API_KEY"), max_retries=1, timeout=120)
        self.max_wait = settings.transcribe.max_wait_seconds

    @property
    def source(self) -> str:
        return f"groq:{self.model}"

    def __call__(self, path: Path, prompt: str) -> dict:
        def call() -> dict:
            with path.open("rb") as f:
                return self.client.audio.transcriptions.create(
                    file=f, model=self.model, response_format="verbose_json",
                    temperature=0, **({"prompt": prompt} if prompt else {}),
                ).model_dump()

        return call_with_backoff(call, provider="groq", max_wait=self.max_wait)


def run_transcribe(conn: sqlite3.Connection, settings: Settings, limit: int | None = None,
                   transcriber=None) -> TranscribeResult:
    cfg = settings.transcribe
    rows = db.pending(conn, "downloaded", settings.pipeline.max_attempts, limit)
    res = TranscribeResult()
    if not rows:
        return res
    transcriber = transcriber or GroqTranscriber(settings)
    with console.status("") as status:
        for i, row in enumerate(rows):
            status.update(f"transcribe {i + 1}/{len(rows)}: {row['url']}")
            fields = {"id": row["id"], "url": row["url"]}
            path = settings.resolve(Path(row["audio_path"] or ""))
            if not row["audio_path"] or not path.is_file():
                db.update(conn, row["id"], state="ingested", audio_path=None)  # download again
                res.redownload += 1
                log.warning("audio missing, queued for re-download", extra={"fields": fields})
                continue
            try:
                out = transcriber(path, build_prompt(row["caption"], row["hashtags"]))
            except StopStage as e:
                res.stopped = str(e)
                log.warning("transcribe stopped", extra={"fields": fields | {"reason": str(e)}})
                break
            except Exception as e:  # API/network errors: count an attempt, move on
                db.fail(conn, row["id"], "transcribe", f"{type(e).__name__}: {e}")
                res.failed += 1
                log.error("transcribe failed", extra={"fields": fields | {"error": str(e)}})
                continue

            text = speech_text(out.get("segments") or [], cfg.no_speech_threshold)
            if not out.get("segments"):  # some responses have no segments; fall back to text
                text = (out.get("text") or "").strip()
            needs_visual = int(word_count(text) < cfg.min_words)
            db.advance(conn, row["id"], "transcribed", transcript=text,
                       transcript_source=transcriber.source, needs_visual=needs_visual)
            res.done += 1
            res.needs_visual += needs_visual
            log.info("transcribed", extra={"fields": fields | {
                "words": word_count(text), "language": out.get("language"), "needs_visual": needs_visual}})
    return res
