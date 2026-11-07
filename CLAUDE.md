# savemap

Personal Python CLI that turns saved TikToks/Instagram Reels into an Obsidian vault mapped by
topic (CS and anything else saved). Free tools / free API tiers only. `SPEC.md` is the source of
truth for behavior; update it when a decision changes.

## Commands
```sh
uv sync                              # install deps
uv run pytest                        # tests
uv run savemap <stage> --limit N     # ingest | download | transcribe | enrich | export
uv run savemap run --limit N         # all stages in order
uv run savemap status                # counts by state + recent errors
```

## Architecture
- `src/savemap/`: one module per stage, wired up in `cli.py`.
- `db.py`: SQLite at `data/savemap.db`, one `videos` row per video. State machine:
  `ingested → downloaded → transcribed → enriched`, plus terminal `unavailable`.
- Every stage follows the same pattern:
  - `db.pending(conn, <prev state>, max_attempts, limit)` → do the work.
  - On success, `db.advance(...)`. On failure, `db.fail(...)`, which bumps `attempts`.
  - Raise `ratelimit.StopStage` on rate limits so the run stops cleanly and resumes next time.
  - Stages must stay idempotent and resumable.
- `taxonomy.py` handles open-ended area → topic grouping: seeds come from `config.toml`, the
  LLM may create new topics, and names are loosely matched and aliased.
- `ingest/ids.py` turns a URL into (platform, video_id), which is the dedupe key.

## Rules
- Model names, paths, delays and thresholds go in `config.toml`, never hardcoded.
- Secrets go only in `.env`. `.env.example` stays empty; `tests/test_repo_hygiene.py` enforces it.
- Never commit `.env`, cookie files, `urls.txt`, `data/`, `exports/` or `vault/`.
- Before writing or changing Groq/Gemini API code, check their current docs. SDKs and model IDs
  change often.
- Test each stage on the ~10 real videos in `urls.txt` with `--limit` before moving on, and add
  unit tests for parsers, schemas and merge logic.
- Work on a feature branch. Commit only when asked.

## Gotchas
- TikTok's yt-dlp `audio` format is the attached music track, not the creator's voice.
  `download.py` pulls audio from the video and checks the audio length against the video.
- Instagram uses Chrome cookies, which triggers a macOS Keychain prompt on first use.
  "Login required" errors usually mean the cookies expired.
- Whisper makes up text on music-only audio. Filter segments by `no_speech_prob`.
- TikTok captions contain invisible placeholder characters for @-mentions; `clean_text()`
  removes them.
- Always pass an explicit timeout to API clients (`google-genai` without one once hung forever).
