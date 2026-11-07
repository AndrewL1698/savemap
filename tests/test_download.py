import json

import pytest

from savemap.download import check_duration, classify, clean_text, extract_metadata


@pytest.mark.parametrize("msg, kind", [
    ("[Instagram] abc: Requested content is not available, rate-limit reached or login required. "
     "Use --cookies, --cookies-from-browser", "login"),
    ("[TikTok] 123: Video unavailable", "unavailable"),
    ("This video is private", "unavailable"),
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("Unable to download webpage: <urlopen error timed out>", "error"),
])
def test_classify(msg, kind):
    assert classify(msg) == kind


def test_clean_text_strips_mention_placeholders():
    assert clean_text("Replying to @￴ ￴￴  Better prompts  #roblox\n") == \
        "Replying to @ Better prompts #roblox"


def test_extract_metadata_merges_tags_and_hashtags():
    info = {"uploader": "musabcodes", "description": "intern tips #FAANG #tech", "tags": ["tech", "career"],
            "duration": 55}
    m = extract_metadata(info)
    assert m["author"] == "musabcodes"
    assert json.loads(m["hashtags"]) == ["tech", "career", "faang"]
    assert m["duration"] == 55


def test_check_duration():
    check_duration(160, 162)    # small drift ok
    check_duration(None, 400)   # unknown -> skip
    with pytest.raises(RuntimeError, match="wrong track"):
        check_duration(160, 415)
