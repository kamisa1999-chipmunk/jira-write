"""Load Confluence connection settings from .env without exposing secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from utils.env import resolve_env_path

from .exceptions import ConfluenceConfigError

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = SCRIPTS_DIR / ".env"
DEFAULT_CONFLUENCE_URL = "https://confluence.goldapple.ru"


@dataclass(frozen=True)
class ConfluenceConfig:
    """Connection settings. Token/password are never printed by __repr__."""

    url: str
    pat: str = ""
    username: str = ""
    password: str = ""
    timeout: int = 30

    def __repr__(self) -> str:
        auth = "pat" if self.pat else ("basic" if self.username else "none")
        return (
            f"ConfluenceConfig(url={self.url!r}, auth={auth!r}, "
            f"timeout={self.timeout})"
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.pat) or bool(self.username and self.password)


def load_confluence_config(env_path: Optional[Path] = None) -> ConfluenceConfig:
    """Read `.env` next to scripts/ and build Confluence config.

    Uses CONFLUENCE_* only — never JIRA_PAT.
    """
    path = resolve_env_path(DEFAULT_ENV_PATH, env_path)
    if path:
        load_dotenv(path)
    else:
        load_dotenv()

    config = ConfluenceConfig(
        url=os.getenv("CONFLUENCE_URL", DEFAULT_CONFLUENCE_URL).rstrip("/"),
        pat=os.getenv("CONFLUENCE_PAT", "").strip(),
        username=os.getenv("CONFLUENCE_USERNAME", "").strip(),
        password=os.getenv("CONFLUENCE_PASSWORD", "").strip(),
    )

    if not config.has_auth:
        raise ConfluenceConfigError(
            "Нужен CONFLUENCE_PAT или пара CONFLUENCE_USERNAME + "
            "CONFLUENCE_PASSWORD в .env (отдельно от JIRA_PAT)"
        )
    return config
