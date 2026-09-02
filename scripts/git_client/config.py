"""Load GitLab/GitHub connection settings from .env without exposing secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from utils.env import resolve_env_path

from .exceptions import GitConfigError

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = SCRIPTS_DIR / ".env"
DEFAULT_GITLAB_URL = "https://git.goldapple.ru"


@dataclass(frozen=True)
class GitConfig:
    """Git provider settings. Token is never printed by __repr__."""

    provider: str  # gitlab | github
    url: str
    pat: str
    timeout: int = 30

    def __repr__(self) -> str:
        return (
            f"GitConfig(provider={self.provider!r}, url={self.url!r}, "
            f"auth={'pat' if self.pat else 'none'!r}, timeout={self.timeout})"
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.pat)


def load_git_config(env_path: Optional[Path] = None) -> GitConfig:
    """Read `.env` next to scripts/ and build Git config.

    Prefers GitLab if `GITLAB_PAT` is set, otherwise GitHub if `GITHUB_PAT` is set.
    Explicit `GIT_PROVIDER=gitlab|github` overrides the preference when that
    provider has a token.
    """
    path = resolve_env_path(DEFAULT_ENV_PATH, env_path)
    if path:
        load_dotenv(path)
    else:
        load_dotenv()

    provider_hint = (os.getenv("GIT_PROVIDER") or "").strip().lower()
    gitlab_url = (os.getenv("GITLAB_URL") or DEFAULT_GITLAB_URL).rstrip("/")
    gitlab_pat = (os.getenv("GITLAB_PAT") or "").strip()
    github_url = (os.getenv("GITHUB_URL") or "https://api.github.com").rstrip("/")
    github_pat = (os.getenv("GITHUB_PAT") or "").strip()

    if provider_hint == "github" and github_pat:
        return GitConfig(provider="github", url=github_url, pat=github_pat)
    if provider_hint == "gitlab" and gitlab_pat:
        return GitConfig(provider="gitlab", url=gitlab_url, pat=gitlab_pat)

    if gitlab_pat:
        return GitConfig(provider="gitlab", url=gitlab_url, pat=gitlab_pat)
    if github_pat:
        return GitConfig(provider="github", url=github_url, pat=github_pat)

    raise GitConfigError(
        "Нужен GITLAB_PAT (или GITHUB_PAT) в .env для --with-git. "
        "Не смешивай с JIRA_PAT."
    )
