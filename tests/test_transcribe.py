import math
from types import SimpleNamespace

import pytest

from savemap import db
from savemap.config import load_settings
from savemap.ratelimit import StopStage, call_with_backoff, rate_limit_wait
from savemap.transcribe import build_prompt, run_transcribe, speech_text

from .test_ingest import ROOT


def test_speech_text_drops_non_speech_segments():
    segs = [
        {"text": " Have you heard of the dead game method?", "no_speech_prob": 0.01},
        {"text": " Thanks for watching!", "no_speech_prob": 0.92},  # hallucination over music
        {"text": "   ", "no_speech_prob": 0.0},
    ]
    assert speech_text(segs, 0.6) == "Have you heard of the dead game method?"


def test_build_prompt_includes_hashtags_and_is_truncated():
    p = build_prompt("Roblox scripting tips", '["roblox", "lua"]')
    assert p == "Roblox scripting tips #roblox #lua"
    assert len(build_prompt("x" * 5000, None)) == 600


def _err(status, msg="", headers=None):
    e = Exception(msg)
    e.status_code = status
    e.response = SimpleNamespace(headers=headers or {})
    return e


def test_rate_limit_wait():
    assert rate_limit_wait(_err(500)) is None
    assert rate_limit_wait(_err(429, headers={"retry-after": "7"})) == 7.0
    assert math.isinf(rate_limit_wait(_err(429, "Rate limit reached ... audio seconds per day (ASD)")))
    assert rate_limit_wait(_err(429, "Please try again in 1m30.5s")) == 90.5
    gem = Exception("429 RESOURCE_EXHAUSTED {'retryDelay': '37s'}")
    gem.code = 429
    assert rate_limit_wait(gem) == 37.0


def test_call_with_backoff_stops_on_long_wait(monkeypatch):
    monkeypatch.setattr("savemap.ratelimit.time.sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise _err(429, headers={"retry-after": "2"})
        return "ok"

    assert call_with_backoff(flaky, provider="groq", max_wait=60) == "ok"
    with pytest.raises(StopStage, match="daily limit"):
        call_with_backoff(lambda: (_ for _ in ()).throw(_err(429, "tokens per day (TPD)")),
                          provider="groq", max_wait=60)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text((ROOT / "config.toml").read_text())
    monkeypatch.delenv("SAVEMAP_CONFIG", raising=False)
    s = load_settings(tmp_path / "config.toml")
    conn = db.connect(s.resolve(s.paths.db))
    audio = tmp_path / "data" / "audio"
    audio.mkdir(parents=True)
    for vid in ("1", "2", "3"):
        db.insert_video(conn, platform="tiktok", video_id=vid, url=f"u{vid}", saved_at=None, source="urls")
    rows = db.pending(conn, "ingested", 3)
    for r in rows[:2]:
        (audio / f"{r['video_id']}.mp3").write_bytes(b"x")
        db.advance(conn, r["id"], "downloaded", audio_path=f"data/audio/{r['video_id']}.mp3")
    db.advance(conn, rows[2]["id"], "downloaded", audio_path="data/audio/missing.mp3")
    return s, conn


class FakeTranscriber:
    source = "fake"

    def __init__(self, outputs):
        self.outputs = outputs

    def __call__(self, path, prompt):
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_run_transcribe_flags_and_resumes(setup):
    s, conn = setup
    words = " ".join(["word"] * 40)
    fake = FakeTranscriber([
        {"segments": [{"text": words, "no_speech_prob": 0.0}]},
        {"segments": [{"text": "Thanks for watching", "no_speech_prob": 0.9}]},
    ])
    res = run_transcribe(conn, s, transcriber=fake)
    assert (res.done, res.needs_visual, res.redownload) == (2, 1, 1)

    rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM videos")}
    assert rows["1"]["state"] == "transcribed" and rows["1"]["needs_visual"] == 0
    assert rows["2"]["transcript"] == "" and rows["2"]["needs_visual"] == 1
    assert rows["3"]["state"] == "ingested"  # missing audio -> back to download
    assert run_transcribe(conn, s, transcriber=FakeTranscriber([])).done == 0  # idempotent


def test_run_transcribe_stops_without_counting_attempt(setup):
    s, conn = setup
    res = run_transcribe(conn, s, transcriber=FakeTranscriber([StopStage("groq rate limit")]))
    assert res.stopped and res.done == 0
    assert all(r["attempts"] == 0 for r in conn.execute("SELECT attempts FROM videos"))
