import json

import pytest
from pydantic import ValidationError

from savemap import db
from savemap.config import Taxonomy as TaxonomyConfig
from savemap.config import load_settings
from savemap.enrich import canonicalize, enrich_one, llm_schema, parse_enrichment, run_enrich
from savemap.llm import ProviderChain
from savemap.ratelimit import StopStage
from savemap.taxonomy import Taxonomy

from .test_ingest import ROOT

TYPES = ["concept", "tutorial", "career", "video_idea"]


def payload(**over) -> dict:
    base = {
        "title": "Use Claude to remake dead games for Roblox",
        "summary": "Pick a shut-down game with good ideas and rebuild it on Roblox with AI help.",
        "key_takeaways": ["Find dead games", "Ask Claude to port mechanics"],
        "content_type": "video_idea",
        "topics": [{"area": "Game dev", "topic": "Roblox"}],
        "tags": ["roblox", "claude"],
        "resources": [{"name": "Roblox Studio", "kind": "tool", "url": None}],
        "actionable": True,
        "action": "List 3 dead games and prototype one in Roblox Studio",
    }
    return base | over


def parse(**over):
    return parse_enrichment(json.dumps(payload(**over)), TYPES)


def test_valid_payload():
    e = parse()
    assert e.content_type == "video_idea" and e.topics[0].topic == "Roblox"


def test_bad_content_type_rejected():
    with pytest.raises(ValidationError, match="content_type"):
        parse(content_type="podcast")


def test_content_type_is_normalized():
    assert parse(content_type="Video idea").content_type == "video_idea"


def test_tags_lowercased_deduped_and_capped():
    e = parse(tags=["#Roblox", "roblox", "Claude AI", "a", "b", "c", "d", "e"])
    assert e.tags == ["roblox", "claude ai", "a", "b", "c", "d"]


def test_long_lists_trimmed_not_rejected():
    e = parse(key_takeaways=[f"t{i}" for i in range(8)], topics=[{"area": "A", "topic": f"T{i}"} for i in range(5)])
    assert len(e.key_takeaways) == 5 and len(e.topics) == 3


def test_missing_topics_and_bad_json_rejected():
    with pytest.raises(ValidationError):
        parse(topics=[])
    with pytest.raises(ValidationError):
        parse_enrichment("{not json", TYPES)


def test_unknown_resource_kind_and_empty_action():
    e = parse(resources=[{"name": "x", "kind": "Podcast", "url": ""}], action="  ", actionable=False)
    assert e.resources[0].kind == "other" and e.resources[0].url is None and e.action is None


def test_canonicalize_maps_aliases_and_dedupes():
    tax = Taxonomy(TaxonomyConfig(content_types=TYPES, areas={"Game dev": ["Roblox"]},
                                  aliases={"Roblox Studio": "Roblox"}))
    e = canonicalize(parse(topics=[{"area": "Software & CS", "topic": "roblox studio"},
                                   {"area": "Game dev", "topic": "Roblox"},
                                   {"area": "Career", "topic": "Internships"}]), tax)
    assert [(t.area, t.topic) for t in e.topics] == [("Game dev", "Roblox"), ("Career", "Internships")]
    assert "Internships" in tax.areas["Career"]


def test_schema_is_strict_mode_compatible():
    def check(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                check(child)
        if node.get("type") == "array":
            check(node["items"])
    check(llm_schema(TYPES))


class FakeLLM:
    name = "fake"

    def __init__(self, outputs):
        self.outputs, self.prompts = list(outputs), []

    def complete_json(self, system, user, schema, schema_name):
        self.prompts.append(user)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text((ROOT / "config.toml").read_text())
    monkeypatch.delenv("SAVEMAP_CONFIG", raising=False)
    s = load_settings(tmp_path / "config.toml")
    conn = db.connect(s.resolve(s.paths.db))
    for vid in ("1", "2"):
        db.insert_video(conn, platform="tiktok", video_id=vid, url=f"u{vid}", saved_at=None, source="urls")
    for r in db.pending(conn, "ingested", 3):
        db.advance(conn, r["id"], "transcribed", transcript="some words", hashtags='["roblox"]', caption="cap")
    return s, conn


def test_retry_once_with_error_feedback(setup):
    s, conn = setup
    row = db.pending(conn, "transcribed", 3)[0]
    llm = FakeLLM(['{"title": "x"}', json.dumps(payload())])
    e = enrich_one(llm, row, Taxonomy(s.taxonomy), s)
    assert e.title.startswith("Use Claude")
    assert "previous answer was invalid" in llm.prompts[1]


def test_run_enrich_reuses_new_topics_and_fails_after_two_bad_answers(setup):
    s, conn = setup
    first = payload(topics=[{"area": "Content creation", "topic": "YouTube growth"}])
    llm = FakeLLM([json.dumps(first), "bad", "still bad"])
    res = run_enrich(conn, s, llm=llm)
    assert (res.done, res.failed, res.new_topics) == (1, 1, 1)
    # The second prompt already lists the topic created for the first video.
    assert "YouTube growth" in llm.prompts[1]
    failed = conn.execute("SELECT * FROM videos WHERE state = 'transcribed'").fetchone()
    assert failed["attempts"] == 1 and failed["error_stage"] == "enrich"


def test_provider_chain_fails_over_then_stops():
    capped = FakeLLM([StopStage("groq rate limit (daily limit reached)")])
    capped.name = "groq"
    backup = FakeLLM(["{}", StopStage("gemini rate limit")])
    backup.name = "gemini"
    chain = ProviderChain([capped, backup])
    assert chain.complete_json("s", "u", {}, "n") == "{}"
    assert chain.name == "gemini"
    with pytest.raises(StopStage, match="all LLM providers"):
        chain.complete_json("s", "u", {}, "n")
