"""Shared rate-limit handling for stages."""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

from .logs import log

T = TypeVar("T")

# Daily caps (Groq RPD/TPD/ASD, Gemini "...PerDay" quota ids): waiting a minute won't help.
_DAILY = re.compile(r"per day|\b(?:RPD|TPD|ASD)\b|PerDay", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retry_?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)
_TRY_AGAIN_IN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", re.IGNORECASE)


class StopStage(Exception):
    """Stop the current stage cleanly (e.g. platform/API rate limit reached).

    The current row is left untouched (no attempt counted) so the next run resumes there.
    """


def rate_limit_wait(exc: BaseException) -> float | None:
    """Seconds to wait if `exc` is a 429 from Groq or Gemini; inf for daily caps; None otherwise."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status != 429:
        return None
    text = f"{exc} {getattr(exc, 'details', '') or ''} {getattr(exc, 'body', '') or ''}"
    if _DAILY.search(text):
        return math.inf
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        if ra := headers.get("retry-after"):
            return float(ra)
    except ValueError:
        pass
    if m := _RETRY_DELAY.search(text):
        return float(m.group(1))
    if (m := _TRY_AGAIN_IN.search(text)) and any(m.groups()):
        h, mi, s = (float(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return 30.0


def call_with_backoff(fn: Callable[[], T], *, provider: str, max_wait: float, retries: int = 4) -> T:
    """Run fn(); on rate limits sleep and retry, or raise StopStage if the wait is too long."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            wait = rate_limit_wait(e)
            if wait is None:
                raise
            if wait > max_wait or attempt == retries:
                why = "daily limit reached" if math.isinf(wait) else f"retry after {wait:.0f}s"
                raise StopStage(f"{provider} rate limit ({why})") from e
            log.info("rate limited, sleeping", extra={"fields": {"provider": provider, "wait": wait}})
            time.sleep(wait + random.uniform(0.5, 2.0))
    raise AssertionError("unreachable")
