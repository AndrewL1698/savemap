"""Identify platform + stable video ID from a URL (used for dedupe)."""

from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit


class VideoRef(NamedTuple):
    platform: str
    video_id: str
    url: str


_TIKTOK_ID = re.compile(r"/(?:video|v)/(\d+)")
_TIKTOK_WEB = re.compile(r"/@[\w.-]+/video/\d+")
_IG_CODE = re.compile(r"/(reels?|p|tv)/([A-Za-z0-9_-]+)")


def normalize_url(url: str) -> str:
    """Drop query string, fragment and trailing slash; force https + lowercase host."""
    parts = urlsplit(url.strip())
    if not parts.scheme:
        parts = urlsplit("https://" + url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def identify(url: str) -> VideoRef:
    norm = normalize_url(url)
    host = urlsplit(norm).netloc.removeprefix("www.").removeprefix("m.")
    path = urlsplit(norm).path

    if host.endswith(("tiktok.com", "tiktokv.com")):
        if m := _TIKTOK_ID.search(path):
            vid = m.group(1)
            if _TIKTOK_WEB.fullmatch(path):
                return VideoRef("tiktok", vid, f"https://www.tiktok.com{path}")
            # Share/mobile formats (tiktokv.com/share/video/ID, m.tiktok.com/v/ID.html).
            return VideoRef("tiktok", vid, f"https://www.tiktok.com/@_/video/{vid}")
        # Short links (vm.tiktok.com/XYZ, tiktok.com/t/XYZ): resolved to the numeric ID at download.
        return VideoRef("tiktok", f"short:{norm}", norm)

    if host.endswith("instagram.com"):
        if m := _IG_CODE.search(path):
            code = m.group(2)
            kind = "reel" if m.group(1) in ("reel", "reels") else m.group(1)  # keep /p/ vs /reel/
            return VideoRef("instagram", code, f"https://www.instagram.com/{kind}/{code}/")
        raise ValueError(f"Unrecognized Instagram URL (expected /reel/<code> or /p/<code>): {url}")

    return VideoRef("other", norm, norm)
