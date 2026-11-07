"""Shared rate-limit handling for stages."""

from __future__ import annotations


class StopStage(Exception):
    """Stop the current stage cleanly (e.g. platform/API rate limit reached).

    The current row is left untouched (no attempt counted) so the next run resumes there.
    """
