"""Stage 2: fetch audio + metadata with yt-dlp, convert to 16 kHz mono mp3 with ffmpeg."""

from __future__ import annotations

import json
import random
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from . import db
from .config import Settings
from .logs import console, log
from .ratelimit import StopStage

# Checked in this order against the lowercased error message. Login comes first because
# Instagram's "rate-limit reached or login required" usually means missing/expired cookies.
_NEEDS_LOGIN = ("login required", "log in to", "--cookies", "could not find chrome cookies",
                "failed to decrypt", "cookie")
_RATE_LIMITED = ("rate-limit", "rate limit", "too many requests", "http error 429", "please wait a few minutes")
_UNAVAILABLE = (
    "video unavailable", "this video is unavailable", "private", "has been removed", "been deleted",
    "no longer available", "not available", "content isn't available", "http error 404",
    "unable to find video", "there is no video in this post", "status code 10204", "10216",
)

_HASHTAG = re.compile(r"#([\w]+)")

# Per-platform yt-dlp format selectors. TikTok's "audio" format is the attached music track,
# not the creator's voice, so take the smallest real video that has sound instead.
_FORMATS = {
    "tiktok": "worst[acodec!=none][vcodec!=none][format_id!=audio]/best[acodec!=none][format_id!=audio]",
}
_DEFAULT_FORMAT = "bestaudio/best"


def clean_text(text: str) -> str:
    """Drop control/unassigned/private-use chars (e.g. TikTok's mention placeholders)."""
    keep = (c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cn", "Co", "Cs"))
    return re.sub(r"[ \t]+", " ", "".join(keep)).strip()


class NotFoundError(Exception):
    pass


@dataclass
class DownloadResult:
    done: int = 0
    unavailable: int = 0
    failed: int = 0
    merged: int = 0
    stopped: str | None = None


def classify(message: str) -> str:
    """'rate_limited' | 'login' | 'unavailable' | 'error'."""
    m = message.lower()
    for kind, patterns in (("login", _NEEDS_LOGIN), ("rate_limited", _RATE_LIMITED),
                           ("unavailable", _UNAVAILABLE)):
        if any(p in m for p in patterns):
            return kind
    return "error"


def extract_metadata(info: dict) -> dict:
    caption = clean_text(info.get("description") or info.get("title") or "")
    tags = [t.lower() for t in (info.get("tags") or []) if t]
    tags += [t.lower() for t in _HASHTAG.findall(caption)]
    return {
        "author": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "caption": caption or None,
        "hashtags": json.dumps(list(dict.fromkeys(tags))),
        "duration": info.get("duration"),
    }


def _ydl_opts(settings: Settings, platform: str, outtmpl: str) -> dict:
    opts = {
        "format": _FORMATS.get(platform, _DEFAULT_FORMAT),
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    dl = settings.download
    if platform == "instagram":
        if dl.instagram_cookies_file:
            opts["cookiefile"] = str(settings.resolve(Path(dl.instagram_cookies_file)))
        elif dl.instagram_cookies_from_browser:
            opts["cookiesfrombrowser"] = (dl.instagram_cookies_from_browser,)
    return opts


def to_mp3(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst)],
        check=True, capture_output=True, text=True,
    )


def audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def check_duration(expected: float | None, actual: float) -> None:
    """Guard against grabbing the wrong track (e.g. a full song instead of the video)."""
    if expected and abs(actual - expected) > max(5.0, 0.15 * expected):
        raise RuntimeError(f"audio is {actual:.0f}s but video is {expected:.0f}s; wrong track?")


def fetch(settings: Settings, row: sqlite3.Row) -> tuple[dict, Path]:
    """Download one video's audio. Returns (info dict, mp3 path)."""
    audio_dir = settings.resolve(settings.paths.audio_dir)
    tmp_dir = audio_dir / "tmp"
    info_dir = audio_dir.parent / "info"
    for d in (tmp_dir, info_dir):
        d.mkdir(parents=True, exist_ok=True)

    with yt_dlp.YoutubeDL(_ydl_opts(settings, row["platform"], str(tmp_dir / f"{row['id']}.%(ext)s"))) as ydl:
        info = ydl.extract_info(row["url"], download=True)
        if info.get("_type") == "playlist":  # carousel post: take the first video entry
            entries = [e for e in info.get("entries") or [] if e and e.get("requested_downloads")]
            if not entries:
                raise NotFoundError("There is no video in this post")
            info = entries[0]
        src = Path(info["requested_downloads"][0]["filepath"])
        (info_dir / f"{row['platform']}_{info.get('id') or row['id']}.json").write_text(
            json.dumps(ydl.sanitize_info(info), ensure_ascii=False, indent=1)
        )

    dst = audio_dir / f"{row['platform']}_{info.get('id') or row['video_id']}.mp3"
    try:
        to_mp3(src, dst)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip()
        if "does not contain any stream" in stderr or "matches no streams" in stderr:
            raise NotFoundError("no audio stream in video") from None
        raise RuntimeError(f"ffmpeg failed: {stderr[-500:]}") from None
    finally:
        src.unlink(missing_ok=True)
    try:
        actual = audio_duration(dst)
        check_duration(info.get("duration"), actual)
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    if not info.get("duration"):  # Instagram sometimes omits it
        info["duration"] = round(actual, 1)
    return info, dst


def _resolve_short_link(conn: sqlite3.Connection, row: sqlite3.Row, info: dict) -> bool:
    """Replace a 'short:' placeholder ID with the real one. Returns False if it's a duplicate."""
    real_id, real_url = info.get("id"), info.get("webpage_url")
    if not row["video_id"].startswith("short:") or not real_id:
        return True
    try:
        db.update(conn, row["id"], video_id=str(real_id), url=real_url or row["url"])
        return True
    except sqlite3.IntegrityError:
        conn.execute("DELETE FROM videos WHERE id = ?", (row["id"],))
        conn.commit()
        return False


def run_download(conn: sqlite3.Connection, settings: Settings, limit: int | None = None) -> DownloadResult:
    if not shutil.which("ffmpeg"):
        raise StopStage("ffmpeg not found on PATH (brew install ffmpeg)")
    rows = db.pending(conn, "ingested", settings.pipeline.max_attempts, limit)
    res = DownloadResult()
    root = settings.root
    with console.status("") as status:
        for i, row in enumerate(rows):
            if i:
                time.sleep(random.uniform(settings.download.delay_min, settings.download.delay_max))
            status.update(f"download {i + 1}/{len(rows)}: {row['url']}")
            fields = {"id": row["id"], "platform": row["platform"], "url": row["url"]}
            try:
                info, mp3 = fetch(settings, row)
            except (DownloadError, NotFoundError, RuntimeError) as e:
                msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).removeprefix("ERROR: ")
                kind = "unavailable" if isinstance(e, NotFoundError) else classify(msg)
                if kind == "rate_limited":
                    res.stopped = f"{row['platform']} rate limit: {msg}"
                    log.warning("download stopped: rate limited", extra={"fields": fields | {"error": msg}})
                    break
                if kind == "unavailable":
                    db.update(conn, row["id"], state="unavailable", error_stage="download", last_error=msg)
                    res.unavailable += 1
                    log.info("unavailable", extra={"fields": fields | {"error": msg}})
                else:
                    if kind == "login":
                        msg = f"login/cookies problem: {msg}"
                    db.fail(conn, row["id"], "download", msg)
                    res.failed += 1
                    log.error("download failed", extra={"fields": fields | {"error": msg}})
                continue

            if not _resolve_short_link(conn, row, info):
                res.merged += 1
                mp3.unlink(missing_ok=True)
                log.info("short link duplicate removed", extra={"fields": fields})
                continue
            db.advance(conn, row["id"], "downloaded",
                       audio_path=str(mp3.relative_to(root)) if mp3.is_relative_to(root) else str(mp3),
                       **extract_metadata(info))
            res.done += 1
            log.info("downloaded", extra={"fields": fields | {"duration": info.get("duration")}})
    return res
