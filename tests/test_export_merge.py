import json
from datetime import date

import pytest

from savemap import db
from savemap.config import load_settings
from savemap.export import MARKER, merge, obsidian_tag, run_export, safe_name

from .test_enrich_schema import payload
from .test_ingest import ROOT


class FakeLLM:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, schema, schema_name):
        self.calls += 1
        return json.dumps({"overview": f"overview #{self.calls}"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text((ROOT / "config.toml").read_text())
    monkeypatch.delenv("SAVEMAP_CONFIG", raising=False)
    s = load_settings(tmp_path / "config.toml")
    conn = db.connect(s.resolve(s.paths.db))

    def add(vid, **over):
        db.insert_video(conn, platform="tiktok", video_id=vid, url=f"https://www.tiktok.com/@a/video/{vid}",
                        saved_at=f"2026-10-0{vid}", source="urls")
        row = conn.execute("SELECT id FROM videos WHERE video_id = ?", (vid,)).fetchone()
        db.advance(conn, row["id"], "enriched", author="a", enrichment=json.dumps(payload(**over)))

    return s, conn, add, tmp_path / "vault"


def test_safe_name_and_tags():
    assert safe_name("AI/ML: what's [new]?") == "AI-ML- what's -new"
    assert obsidian_tag("claude ai") == "claude-ai" and obsidian_tag("2027") == "t-2027"


def test_merge_keeps_user_section_and_reviewed(tmp_path):
    p = tmp_path / "n.md"
    assert merge(p, "gen") == (f"gen\n\n{MARKER}\n", None)
    p.write_text(f"---\nreviewed: true\n---\nold body\n\n{MARKER}\nmy thoughts\n")
    assert merge(p, "new body") == (f"new body\n\n{MARKER}\nmy thoughts\n", True)


def test_export_end_to_end_preserves_edits(env):
    s, conn, add, vault = env
    add("1")
    add("2", title="AI resume system", topics=[{"area": "Career", "topic": "Job search"},
                                              {"area": "Software & CS", "topic": "AI/ML"}])
    llm = FakeLLM()
    res = run_export(conn, s, llm=llm, today=date(2026, 11, 7))
    assert (res.videos, res.topics, res.areas) == (2, 3, 3)
    assert res.overviews_generated == 3

    note = vault / "Videos" / "Use Claude to remake dead games for Roblox (1).md"
    text = note.read_text()
    assert "reviewed: false" in text and "[[Topics/Roblox|Roblox]]" in text
    assert (vault / "Topics" / "AI-ML.md").exists()
    assert "[[Areas/Game dev|Game dev]]" in (vault / "Index.md").read_text()
    assert "Use Claude to remake" in (vault / "Review" / "This week.md").read_text()

    # User edits: ticks reviewed and writes notes below the marker.
    note.write_text(text.replace("reviewed: false", "reviewed: true") + "I tried this, it works.\n")
    res2 = run_export(conn, s, llm=llm, today=date(2026, 11, 8))
    text2 = note.read_text()
    assert "reviewed: true" in text2 and text2.endswith("I tried this, it works.\n")
    assert res2.overviews_generated == 0  # counts unchanged -> cached
    assert "Use Claude to remake" not in (vault / "Review" / "This week.md").read_text()

    # Stable names: a later title change doesn't create a second note.
    conn.execute("UPDATE videos SET enrichment = json_set(enrichment, '$.title', 'Renamed') WHERE video_id='1'")
    run_export(conn, s, llm=llm)
    assert len(list((vault / "Videos").glob("*.md"))) == 2


def test_aliases_apply_at_export(env):
    s, conn, add, vault = env
    add("1", topics=[{"area": "Game dev", "topic": "Roblox Studio"}])
    s.taxonomy.aliases["Roblox Studio"] = "Roblox"
    run_export(conn, s, overviews=False)
    assert (vault / "Topics" / "Roblox.md").exists()
    assert not (vault / "Topics" / "Roblox Studio.md").exists()


def test_review_pick_is_stable_within_week(env):
    s, conn, add, vault = env
    for i in range(1, 9):
        add(str(i), title=f"Video number {i}")
    picks = []
    for day in (date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 9)):  # Mon, Sun, next Mon
        run_export(conn, s, overviews=False, today=day)
        picks.append((vault / "Review" / "This week.md").read_text().split("\n", 1)[1])
    assert picks[0] == picks[1]
    assert picks[1] != picks[2]
