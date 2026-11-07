# savemap — turn saved TikToks & Reels into a topic map

## Goal
A local Python CLI that takes my TikTok and Instagram saved videos, transcribes
and summarizes each one, groups them into topics (CS and anything else I save), and outputs an
Obsidian vault whose graph view is a map of everything I've saved.

Everything must run on free tools or free API tiers. No paid services.

## Stack
- Python 3.11+, managed with `uv`
- CLI: `typer`; storage: SQLite (single file, `data/savemap.db`)
- Download: `yt-dlp` (audio-only where possible) + `ffmpeg`
- Transcription: Groq Whisper API (free tier), fallback `faster-whisper` locally
- Low-speech / on-screen-code videos: Gemini API free tier with video input
- Summarization/classification: Gemini free tier (default) or Groq-hosted
  open model; must be swappable via config. Optional: Ollama for fully local.
- Embeddings: `sentence-transformers` locally (e.g. all-MiniLM-L6-v2)
- Clustering: HDBSCAN (or BERTopic if simpler)
- Output: Markdown files for Obsidian; optional HTML graph (pyvis or Cytoscape.js)

IMPORTANT: Before writing API code, check the current docs for Groq and Gemini
for model names, SDK usage, and free-tier rate limits. Do not hardcode model
names; put them in `config.toml`.

## Inputs
1. TikTok data export (JSON) — contains favorite/saved videos with links + dates.
2. Instagram data export (JSON) — contains saved posts with links + dates.
3. Optional: `urls.txt`, one URL per line, for manual additions.

The export file structures change over time. Inspect the actual files in
`exports/` and write parsers based on what's really there. Fail loudly with a
helpful message if the expected keys are missing.

## Pipeline stages
Each stage is its own CLI command, reads/writes SQLite, is idempotent, and
skips items already done. `savemap run` runs all stages in order.
Every stage must be resumable after a crash or rate-limit stop.

1. `ingest` — parse exports → insert rows (dedupe on platform + video ID).
2. `download` — yt-dlp: audio-only + `--write-info-json` for caption, author,
   hashtags, duration. Instagram uses a cookie file path from config
   (never commit it; add to .gitignore). Random 2–5s delay between requests.
   Mark deleted/private videos as `unavailable` and move on.
3. `transcribe` — Groq Whisper; on rate limit, back off and retry; if Groq is
   unavailable, fall back to local faster-whisper. Record `transcript_source`.
   If the transcript is under ~30 words, flag `needs_visual`.
4. `visual` — for `needs_visual` items: download the video file, send it to
   Gemini, ask for a description of on-screen text/code and what's being
   taught. Delete the video file afterward.
5. `enrich` — LLM call per video, returning strict JSON (validate with
   pydantic, retry once on invalid JSON):
   - `title` (short, descriptive — not the creator's clickbait)
   - `summary` (2–3 sentences)
   - `key_takeaways` (list, max 5)
   - `content_type`: one of `taxonomy.content_types` in config (default: concept |
     tool | tutorial | how_to | reference | career | interview_prep | project_idea |
     video_idea | inspiration | news | humor)
   - `topics` (1–3 `{area, topic}` pairs; reuse existing ones, create new ones
     only when nothing fits; see Taxonomy below)
   - `tags` (free-form, lowercase, max 6)
   - `resources` (repos, libraries, sites, books mentioned)
   - `actionable` (bool) + `action` (one line: what I could actually do)
6. `organize` — embed `title + summary + takeaways`; cluster; have the LLM
   name each cluster; refine the area → topic → subtopic hierarchy (suggest
   merges/splits as config aliases). Detect
   near-duplicates (cosine similarity > 0.9) and group them.
7. `export` — write the Obsidian vault (see below).

## Taxonomy (open-ended, seeded from config)
Two levels: **area → topic**. Config holds seed areas/topics; enrich shows the LLM
every area/topic that exists so far and it must reuse one when it fits, creating a
new topic (or area) only when nothing does. Names are matched loosely (case,
spacing, "&"/"and") and through `[taxonomy.aliases]` for manual merges/renames.
Each topic belongs to exactly one area; topic names are unique.

Seed areas:
- Software & CS: Data structures & algorithms, System design, Backend, Frontend,
  Databases, DevOps & cloud, Languages, AI/ML, Security, Dev tools & productivity,
  Git, Computer science fundamentals, Project ideas
- Game dev: Roblox
- Content creation: Video ideas, Niches
- Career: Job search, Interview prep
- Other

## Obsidian output (`vault/`)
- `Videos/<slug>.md` — one note per video: frontmatter (url, platform, author,
  saved date, content_type, tags, actionable, reviewed: false), then summary,
  takeaways, resources, link to original, links to `[[Topic]]` notes, and
  "Similar:" links to near-duplicates.
- `Areas/<Area>.md` — hub notes linking their topics, with video counts.
- `Topics/<Topic>.md` — hub notes linking their area, videos and subtopics, with a
  one-paragraph LLM overview of what I've saved on that topic.
- `Index.md` — top-level map: areas → topics with counts.
- `Review/This week.md` — 5 random unreviewed actionable videos.
Re-running export must update notes without destroying edits I made below a
`<!-- my notes -->` marker.

## Config & secrets
- `config.toml` for models, paths, delays, thresholds, taxonomy.
- `.env` for `GROQ_API_KEY`, `GEMINI_API_KEY`. Never commit either.

## Quality bar
- Progress bars (`rich`) and a final summary of counts per stage/status.
- Structured logging to `data/savemap.log`.
- `savemap status` shows counts by stage and any errors.
- Tests for the export parsers and the JSON schema validation.
- `--limit N` flag on every stage so I can test on 10 videos first.

## Build order
Build and verify one stage at a time, testing each on 5–10 real videos
before moving on: ingest → download → transcribe → enrich → export
(get a working vault end to end), THEN add visual, organize, and the
optional HTML graph.