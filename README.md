# savemap

Turn saved TikToks & Reels into an Obsidian vault mapped by CS topic. See `SPEC.md`.

## Setup
```sh
brew install ffmpeg
uv sync
cp .env.example .env   # add GROQ_API_KEY (and GEMINI_API_KEY if using Gemini)
```

## Usage
```sh
uv run savemap ingest --limit 10   # urls.txt (+ exports/ once parsers exist)
uv run savemap status
uv run savemap run --limit 10      # all stages in order
uv run pytest
```
Every stage skips work already done and can be re-run after a crash or rate limit.
`--limit N` is available on every stage.

Inputs: put one URL per line in `urls.txt`, and drop platform exports (JSON) in `exports/`.
Both are gitignored, along with `.env`, cookie files, `data/` and `vault/`.
