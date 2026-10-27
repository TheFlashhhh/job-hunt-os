"""Application settings and path resolution.

Secrets come from the environment. User-editable career policy comes from
``data/config/*.yaml`` — never hard-coded in services.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_DIR = DATA_DIR / "config"
GENERATED_DIR = DATA_DIR / "generated"
CONTEXT_DIR = REPO_ROOT / "context"

#: Where an API operator can reach the project when no personal contact is
#: configured. Sent in the User-Agent; never a person's address by default.
PROJECT_URL = "https://github.com/TheFlashhhh/job-hunt-os"


class Settings(BaseSettings):
    """Environment-backed settings. Secrets live here; policy does not."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    apify_token: str = ""

    jobhunt_db_url: str = f"sqlite:///{REPO_ROOT / 'jobhunt.db'}"
    jobhunt_env: str = "local"
    jobhunt_log_level: str = "INFO"
    #: Contact sent in the User-Agent to job boards (an email or URL). Kept in
    #: the environment so no address is hard-coded into the source.
    jobhunt_http_contact: str = ""

    @property
    def llm_available(self) -> bool:
        """True when an LLM provider can actually be reached.

        When False the pipeline runs DETERMINISTIC_ONLY: every deterministic
        rule still applies and LLM-dependent enrichment is skipped and flagged.
        """
        return bool(self.anthropic_api_key.strip())

    @property
    def apify_available(self) -> bool:
        return bool(self.apify_token.strip())


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def user_agent(version: str) -> str:
    """Identifies this client to the sources it talks to.

    A public API operator who wants to contact the author of unusual traffic
    should be able to: the configured contact, else the project's own page.
    """
    contact = get_settings().jobhunt_http_contact.strip() or PROJECT_URL
    return f"JobHuntOS/{version} (personal job-search tool; contact: {contact})"


def load_yaml_config(name: str) -> dict[str, Any]:
    """Load a user-editable config file from ``data/config/``.

    Missing files return ``{}`` so a fresh checkout runs before every optional
    config exists.
    """
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def test_db_url() -> str:
    """In-memory database URL used by the test suite."""
    return os.environ.get("JOBHUNT_TEST_DB_URL", "sqlite:///:memory:")
