from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, read from the environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "SpecForge"

    # Async driver for the app; Alembic derives a sync URL from this (see sync_database_url).
    database_url: str = "sqlite+aiosqlite:///./signal.db"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    # R1: render + log issue bodies only unless explicitly enabled.
    github_sync_enabled: bool = False

    @property
    def sync_database_url(self) -> str:
        """The synchronous SQLAlchemy URL used by Alembic migrations."""
        return self.database_url.replace("+aiosqlite", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
