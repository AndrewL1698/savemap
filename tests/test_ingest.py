from pathlib import Path

import pytest

from savemap import db
from savemap.config import load_settings
from savemap.ingest import run_ingest
from savemap.ingest.ids import identify, normalize_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url, platform, vid",
    [
        ("https://www.tiktok.com/@someone/video/7312345678901234567?is_from_webapp=1", "tiktok", "7312345678901234567"),
        ("https://www.tiktokv.com/share/video/7312345678901234567/", "tiktok", "7312345678901234567"),
        ("https://m.tiktok.com/v/7312345678901234567.html", "tiktok", "7312345678901234567"),
        ("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc", "instagram", "C1a2B3c4D5e"),
        ("https://instagram.com/p/C1a2B3c4D5e/", "instagram", "C1a2B3c4D5e"),
        ("https://www.instagram.com/reels/C1a2B3c4D5e/", "instagram", "C1a2B3c4D5e"),
    ],
)
def test_identify(url, platform, vid):
    ref = identify(url)
    assert (ref.platform, ref.video_id) == (platform, vid)


def test_original_link_shape_is_kept():
    assert identify("https://www.tiktok.com/@musab.codes/video/123?lang=en").url == \
        "https://www.tiktok.com/@musab.codes/video/123"
    assert identify("https://www.tiktokv.com/share/video/123/").url == "https://www.tiktok.com/@_/video/123"
    assert identify("https://www.instagram.com/p/DfZm0xxM7nD/?hl=en").url == \
        "https://www.instagram.com/p/DfZm0xxM7nD/"
    assert identify("https://www.instagram.com/reels/AbC/").url == "https://www.instagram.com/reel/AbC/"


def test_tiktok_short_link_kept_for_resolution():
    ref = identify("https://vm.tiktok.com/ZMabc123/?x=1")
    assert ref.platform == "tiktok"
    assert ref.video_id == "short:https://vm.tiktok.com/ZMabc123"


def test_instagram_profile_url_rejected():
    with pytest.raises(ValueError, match="Instagram"):
        identify("https://www.instagram.com/someuser/")


def test_normalize_url():
    assert normalize_url("WWW.Example.com/a/b/?q=1#frag") == "https://www.example.com/a/b"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text((ROOT / "config.toml").read_text())
    monkeypatch.delenv("SAVEMAP_CONFIG", raising=False)
    return load_settings(cfg)


def test_ingest_urls_dedupes_and_is_idempotent(settings, tmp_path):
    (tmp_path / "urls.txt").write_text(
        "# my saves\n"
        "https://www.tiktok.com/@a/video/111\n"
        "https://www.tiktokv.com/share/video/111/\n"   # same video, different link format
        "\n"
        "https://www.instagram.com/reel/AbC/\n"
        "https://www.instagram.com/someuser/\n"        # not a post -> warning
    )
    conn = db.connect(settings.resolve(settings.paths.db))

    res = run_ingest(conn, settings)
    assert (res.found, res.inserted, res.duplicates) == (3, 2, 1)
    assert len(res.warnings) == 1 and "urls.txt:6" in res.warnings[0]

    again = run_ingest(conn, settings)
    assert (again.inserted, again.duplicates) == (0, 3)


def test_ingest_limit(settings, tmp_path):
    (tmp_path / "urls.txt").write_text("\n".join(f"https://www.tiktok.com/@a/video/{i}" for i in range(10)))
    conn = db.connect(settings.resolve(settings.paths.db))
    assert run_ingest(conn, settings, limit=3).inserted == 3
    assert run_ingest(conn, settings).inserted == 7


def test_pending_respects_attempts(settings):
    conn = db.connect(settings.resolve(settings.paths.db))
    db.insert_video(conn, platform="tiktok", video_id="1", url="u", saved_at=None, source="urls")
    (row,) = db.pending(conn, "ingested", max_attempts=2)
    db.fail(conn, row["id"], "download", "boom")
    assert len(db.pending(conn, "ingested", max_attempts=2)) == 1
    db.fail(conn, row["id"], "download", "boom again")
    assert db.pending(conn, "ingested", max_attempts=2) == []

    db.advance(conn, row["id"], "downloaded")
    (row,) = db.pending(conn, "downloaded", max_attempts=2)
    assert row["attempts"] == 0 and row["last_error"] is None


FIXTURES = Path(__file__).parent / "fixtures"


def test_instagram_saved_posts_export():
    from savemap.ingest.instagram import load_json, parse_instagram_saved

    items, warnings = parse_instagram_saved(load_json(FIXTURES / "instagram_saved_posts.json"))
    assert [(i.video_id, i.url, i.saved_at, i.source) for i in items[:2]] == [
        ("AAAAAAAAAA1", "https://www.instagram.com/reel/AAAAAAAAAA1/", "2026-11-07T01:35:58Z", "instagram_export"),
        ("BBBBBBBBBB2", "https://www.instagram.com/p/BBBBBBBBBB2/", "2025-01-29T16:34:12Z", "instagram_export"),
    ]
    assert len(items) == 3  # the duplicate is deduped later by the DB
    assert len(warnings) == 1 and "no URL" in warnings[0]


def test_instagram_old_layout_and_unknown():
    from savemap.ingest.instagram import parse_instagram_saved
    from savemap.ingest.models import ExportFormatError

    old = {"saved_saved_media": [{"title": "someone", "string_map_data": {
        "Saved on": {"href": "https://www.instagram.com/reel/CCC/", "timestamp": 1738168452}}}]}
    items, _ = parse_instagram_saved(old)
    assert items[0].video_id == "CCC" and items[0].saved_at == "2025-01-29T16:34:12Z"
    with pytest.raises(ExportFormatError):
        parse_instagram_saved({"something": "else"})


def test_export_backfills_saved_at(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.insert_video(conn, platform="instagram", video_id="X", url="u", saved_at=None, source="urls")
    assert not db.insert_video(conn, platform="instagram", video_id="X", url="u",
                               saved_at="2025-01-01T00:00:00Z", source="instagram_export")
    assert tuple(conn.execute("SELECT saved_at, source FROM videos").fetchone()) == ("2025-01-01T00:00:00Z", "urls")
