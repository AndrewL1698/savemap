from pathlib import Path

from savemap.config import Taxonomy as TaxonomyConfig
from savemap.config import load_settings
from savemap.taxonomy import Taxonomy, name_key

ROOT = Path(__file__).resolve().parents[1]


def cfg(**overrides) -> TaxonomyConfig:
    base = dict(
        content_types=["concept"],
        areas={"Software & CS": ["Dev tools & productivity", "C++"], "Game dev": ["Roblox"]},
        aliases={"roblox studio": "Roblox", "Gamedev": "Game dev"},
    )
    return TaxonomyConfig(**(base | overrides))


def test_project_config_loads():
    s = load_settings(ROOT / "config.toml")
    assert "Game dev" in s.taxonomy.areas
    assert "video_idea" in s.taxonomy.content_types


def test_name_key_is_loose_but_keeps_symbols():
    assert name_key("Dev Tools and   Productivity") == name_key("dev tools & productivity")
    assert name_key("C++") != name_key("C#") != name_key("C")


def test_existing_topic_is_reused_with_its_original_area():
    t = Taxonomy(cfg())
    # LLM put Roblox under the wrong area and with odd casing.
    assert t.canonical("Software & CS", "  roblox ") == ("Game dev", "Roblox")
    assert t.canonical("Software & CS", "dev tools and productivity") == (
        "Software & CS", "Dev tools & productivity")


def test_aliases_apply_to_topics_and_areas():
    t = Taxonomy(cfg())
    assert t.canonical("Gamedev", "Roblox Studio") == ("Game dev", "Roblox")
    assert t.canonical("GAMEDEV", "Unity") == ("Game dev", "Unity")


def test_new_topics_and_areas_are_created_and_remembered():
    t = Taxonomy(cfg())
    assert t.canonical("Fitness", "Calisthenics") == ("Fitness", "Calisthenics")
    assert t.canonical("Health", "calisthenics") == ("Fitness", "Calisthenics")
    assert t.areas["Fitness"] == ["Calisthenics"]
    assert "- Fitness: Calisthenics" in t.prompt_listing()


def test_seen_pairs_from_previous_runs_are_loaded():
    t = Taxonomy(cfg(), seen=[("Content creation", "Video ideas")])
    assert t.canonical("Other", "video ideas") == ("Content creation", "Video ideas")


def test_empty_seed_area_is_listed():
    t = Taxonomy(cfg(areas={"Other": []}))
    assert t.prompt_listing() == "- Other: (no topics yet)"
