import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_has_no_values():
    """Secrets belong in .env (gitignored), never in the tracked .env.example."""
    lines = (ROOT / ".env.example").read_text().splitlines()
    filled = [l.split("=", 1)[0] for l in lines if re.match(r"^\s*[A-Z_]+\s*=\s*\S", l)]
    assert not filled, f"Move these values from .env.example to .env: {filled}"


def test_secret_files_are_gitignored():
    for name in (".env", "cookies.txt", "urls.txt", "data/x", "exports/x", "vault/x"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT)
        assert r.returncode == 0, f"{name} is not gitignored"
