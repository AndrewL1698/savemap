"""savemap CLI. Each stage is idempotent and resumable; `run` chains them."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Optional

import typer
from rich.table import Table

from . import db
from .config import ConfigError, Settings, load_settings
from .download import run_download
from .ingest import run_ingest
from .logs import console, setup_logging
from .ratelimit import StopStage
from .transcribe import run_transcribe

app = typer.Typer(help="Turn saved TikToks & Reels into an Obsidian topic map.", no_args_is_help=True)

Limit = Annotated[Optional[int], typer.Option("--limit", "-n", min=1, help="Process at most N items.")]


def _open() -> tuple[Settings, sqlite3.Connection]:
    try:
        settings = load_settings()
    except ConfigError as e:
        console.print(f"[red]Config error:[/] {e}")
        raise typer.Exit(2)
    setup_logging(settings.resolve(settings.paths.log))
    return settings, db.connect(settings.resolve(settings.paths.db))


def _ingest(settings: Settings, conn: sqlite3.Connection, limit: int | None) -> None:
    res = run_ingest(conn, settings, limit)
    for w in res.warnings:
        console.print(f"[yellow]warning:[/] {w}")
    console.print(
        f"[bold]ingest[/]: found {res.found}, inserted {res.inserted}, already known {res.duplicates}"
    )


def _download(settings: Settings, conn: sqlite3.Connection, limit: int | None) -> None:
    try:
        res = run_download(conn, settings, limit)
    except StopStage as e:
        console.print(f"[red]download stopped:[/] {e}")
        return
    line = (f"[bold]download[/]: done {res.done}, unavailable {res.unavailable}, "
            f"failed {res.failed}")
    if res.merged:
        line += f", short-link duplicates removed {res.merged}"
    console.print(line)
    if res.stopped:
        console.print(f"[yellow]stopped early ({res.stopped}). Re-run later to resume.[/]")


def _transcribe(settings: Settings, conn: sqlite3.Connection, limit: int | None) -> None:
    try:
        res = run_transcribe(conn, settings, limit)
    except ConfigError as e:
        console.print(f"[red]transcribe:[/] {e}")
        return
    line = (f"[bold]transcribe[/]: done {res.done} (needs_visual {res.needs_visual}), "
            f"failed {res.failed}")
    if res.redownload:
        line += f", audio missing -> re-download {res.redownload}"
    console.print(line)
    if res.stopped:
        console.print(f"[yellow]stopped early ({res.stopped}). Re-run later to resume.[/]")


@app.command()
def ingest(limit: Limit = None) -> None:
    """Parse exports + urls.txt and add new videos to the database."""
    settings, conn = _open()
    _ingest(settings, conn, limit)


@app.command()
def download(limit: Limit = None) -> None:
    """Download audio + metadata for ingested videos (yt-dlp + ffmpeg)."""
    settings, conn = _open()
    _download(settings, conn, limit)


@app.command()
def transcribe(limit: Limit = None) -> None:
    """Transcribe downloaded audio with Groq Whisper."""
    settings, conn = _open()
    _transcribe(settings, conn, limit)


@app.command()
def run(limit: Limit = None) -> None:
    """Run all stages in order."""
    settings, conn = _open()
    _ingest(settings, conn, limit)
    _download(settings, conn, limit)
    _transcribe(settings, conn, limit)
    _print_status(conn)


@app.command()
def status() -> None:
    """Show counts by state and recent errors."""
    _, conn = _open()
    _print_status(conn)


def _print_status(conn: sqlite3.Connection) -> None:
    rows = db.counts_by_state(conn)
    if not rows:
        console.print("No videos yet. Add URLs to urls.txt (or exports to exports/) and run `savemap ingest`.")
        return
    platforms = sorted({r["platform"] for r in rows})
    counts = {(r["platform"], r["state"]): r["n"] for r in rows}

    table = Table(title="savemap status")
    table.add_column("state")
    for p in platforms:
        table.add_column(p, justify="right")
    table.add_column("total", justify="right", style="bold")
    for state in db.STATES:
        vals = [counts.get((p, state), 0) for p in platforms]
        if any(vals):
            table.add_row(state, *map(str, vals), str(sum(vals)))
    totals = [sum(counts.get((p, s), 0) for s in db.STATES) for p in platforms]
    table.add_row("[bold]total[/]", *map(str, totals), str(sum(totals)))
    console.print(table)

    errors = db.recent_errors(conn)
    if errors:
        et = Table(title="Recent errors", show_lines=False)
        for col in ("id", "platform", "stage", "tries", "error"):
            et.add_column(col, overflow="fold")
        for e in errors:
            et.add_row(str(e["id"]), e["platform"], e["error_stage"] or "", str(e["attempts"]),
                       (e["last_error"] or "")[:200])
        console.print(et)


if __name__ == "__main__":
    app()
