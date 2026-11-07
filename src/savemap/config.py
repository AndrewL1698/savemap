"""Load config.toml + .env into typed settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(Exception):
    pass


class Paths(BaseModel):
    db: Path = Path("data/savemap.db")
    log: Path = Path("data/savemap.log")
    audio_dir: Path = Path("data/audio")
    exports_dir: Path = Path("exports")
    urls_file: Path = Path("urls.txt")
    vault_dir: Path = Path("vault")


class Pipeline(BaseModel):
    max_attempts: int = Field(3, ge=1)


class Download(BaseModel):
    delay_min: float = Field(2.0, ge=0)
    delay_max: float = Field(5.0, ge=0)
    instagram_cookies_file: str = ""
    instagram_cookies_from_browser: str = ""


class Taxonomy(BaseModel):
    content_types: list[str] = Field(min_length=1)
    areas: dict[str, list[str]] = Field(min_length=1)  # seed area -> seed topics
    aliases: dict[str, str] = {}                       # produced name -> canonical name


class Settings(BaseModel):
    root: Path
    paths: Paths = Paths()
    pipeline: Pipeline = Pipeline()
    download: Download = Download()
    taxonomy: Taxonomy

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else self.root / p

    def secret(self, name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is not set. Add it to {self.root / '.env'} (see .env.example).")
        return value


def find_config(start: Path | None = None) -> Path:
    """Use $SAVEMAP_CONFIG, else the nearest config.toml in cwd or its parents."""
    if env := os.environ.get("SAVEMAP_CONFIG"):
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "config.toml").is_file():
            return d / "config.toml"
    raise ConfigError("No config.toml found in this directory or any parent (or set SAVEMAP_CONFIG).")


def load_settings(path: Path | None = None) -> Settings:
    path = path or find_config()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from None
    load_dotenv(path.parent / ".env")
    return Settings(root=path.parent, **raw)
