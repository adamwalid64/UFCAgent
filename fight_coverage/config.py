"""Local YouTube coverage settings; credentials are environment references only."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Channel(Settings):
    channel_id: str | None = Field(default=None, pattern=r"^UC[A-Za-z0-9_-]{22}$")
    handle: str | None = Field(default=None, pattern=r"^@[^ /?&#]+$")
    name: str
    type: Literal["prediction", "technical breakdown", "technical breakdown / analysis"] = "prediction"
    active: bool = True
    credibility_weight: float = Field(default=1.0, ge=0, le=10, allow_inf_nan=False)
    # These are declarations of actual permissions, not permission granted by this app.
    transcript_access_authorized: bool = False
    audio_download_authorized: bool = False

    @model_validator(mode="after")
    def identity(self):
        if not self.channel_id and not self.handle:
            raise ValueError("Register a channel ID or handle")
        return self


class MCPSettings(Settings):
    transport: str = Field(default="stdio", pattern=r"^(stdio|streamable_http)$")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, description="Child variable -> parent environment variable")
    url_env: str = "YOUTUBE_MCP_URL"
    token_env: str | None = None
    tool: str | None = None
    video_argument: str = "video_id"
    video_as_url: bool = False
    arguments: dict = Field(default_factory=dict)
    result_path: list[str] = Field(default_factory=list)


class CoverageConfig(Settings):
    enabled: bool = False
    sources_path: Path = ROOT / "config/sentiment_sources.yaml"
    channels: list[Channel] = Field(default_factory=list)
    cache_dir: Path = ROOT / "data_cache/youtube"
    api_key_env: str = "YOUTUBE_API_KEY"
    proxy_env: str | None = "YOUTUBE_TRANSCRIPT_PROXY"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    discovery_ttl_hours: float = Field(default=6, gt=0, le=720)
    event_timezone: str = "UTC"
    retention_days: int = Field(default=30, ge=1, le=30)
    daily_quota_units: int = Field(default=500, ge=1, le=10000)
    lookback_days: int = Field(default=21, ge=1, le=365)
    max_pages_per_channel: int = Field(default=4, ge=1, le=100)
    max_videos_per_fight: int = Field(default=6, ge=1, le=30)
    max_candidates_per_fight: int = Field(default=30, ge=1, le=100)
    max_excerpt_chars: int = Field(default=12000, ge=500, le=100000)
    request_timeout: float = Field(default=30, gt=0, le=120)
    retries: int = Field(default=2, ge=0, le=5)
    backoff_seconds: float = Field(default=1, ge=0, le=30)
    failure_ttl_hours: float = Field(default=6, gt=0, le=168)
    mcp: MCPSettings | None = None
    whisper_enabled: bool = False
    whisper_model: str = "base"
    whisper_timeout: int = Field(default=900, ge=30, le=3600)
    max_audio_seconds: int = Field(default=7200, ge=60, le=14400)
    non_youtube: "NonYouTubeConfig" = Field(default_factory=lambda: NonYouTubeConfig())

    @model_validator(mode="after")
    def valid_event_timezone(self):
        try:
            ZoneInfo(self.event_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("event_timezone must be a valid IANA timezone, such as America/New_York") from None
        return self


class WrittenOutlet(Settings):
    name: str
    active: bool = False
    urls: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    automated_access_allowed: bool = False
    terms_url: str = ""
    entry_selector: str = ".staff-pick, .expert-pick"
    writer_selector: str = "[rel=author]"
    reasoning_selector: str = "p"
    pick_selector: str = ".pick"
    writer_weights: dict[str, float] = Field(default_factory=dict)


class RedditConfig(Settings):
    enabled: bool = False
    automated_access_allowed: bool = False
    terms_url: str = "https://redditinc.com/policies/data-api-terms"
    token_env: str = "REDDIT_ACCESS_TOKEN"
    thread_urls: list[str] = Field(default_factory=list)
    top_comments: int = Field(default=5, ge=1, le=20)
    min_score: int = Field(default=5, ge=1)
    credibility_weight: float = Field(default=0.2, gt=0, le=0.5)
    writer_weights: dict[str, float] = Field(default_factory=dict)


class NonYouTubeConfig(Settings):
    written_enabled: bool = False
    outlets: list[WrittenOutlet] = Field(default_factory=list)
    reddit: RedditConfig = Field(default_factory=RedditConfig)


CoverageConfig.model_rebuild()


def load_registry(path: str | Path) -> list[Channel]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"sources"} or not isinstance(data["sources"], list):
        raise ValueError("Source registry requires a sources list")
    channels = [Channel.model_validate(item) for item in data["sources"]]
    if any(not c.channel_id for c in channels):
        raise ValueError("Registry sources require verified channel IDs; handles alone are insufficient")
    if len({c.channel_id for c in channels}) != len(channels):
        raise ValueError("Duplicate source channel IDs")
    return channels


def load_config(path: str | Path | None = None) -> CoverageConfig:
    load_dotenv(ROOT / ".env", override=False)
    explicit = path or os.getenv("YOUTUBE_COVERAGE_CONFIG")
    path = Path(explicit) if explicit else ROOT / "config/youtube_coverage.json"
    if not path.exists() and not explicit:
        return CoverageConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "channels" in raw:
        raise ValueError("Move channels from YouTube JSON settings to config/sentiment_sources.yaml")
    config = CoverageConfig.model_validate(raw)
    if not config.sources_path.is_absolute():
        config.sources_path = (ROOT / config.sources_path).resolve()
    config.channels = load_registry(config.sources_path)
    if not config.cache_dir.is_absolute():
        config.cache_dir = (ROOT / config.cache_dir).resolve()
    if not config.languages:
        raise ValueError("At least one transcript language is required")
    if len({c.channel_id or c.handle.casefold() for c in config.channels}) != len(config.channels):
        raise ValueError("Duplicate YouTube channel IDs")
    return config
