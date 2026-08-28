"""Load Jira connection settings from .env without exposing secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .exceptions import JiraConfigError

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = SCRIPTS_DIR / ".env"
DEFAULT_JIRA_URL = "https://jira01.goldapple.ru"
DEFAULT_PROJECT = "CAT2"


@dataclass(frozen=True)
class JiraConfig:
    """Connection settings. Token/password are never printed by __repr__."""

    url: str
    project: str
    pat: str = ""
    username: str = ""
    password: str = ""
    board_id: str = ""
    timeout: int = 30

    def __repr__(self) -> str:
        auth = "pat" if self.pat else ("basic" if self.username else "none")
        return (
            f"JiraConfig(url={self.url!r}, project={self.project!r}, "
            f"auth={auth!r}, board_id={self.board_id!r}, timeout={self.timeout})"
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.pat) or bool(self.username and self.password)


def load_config(
    env_path: Optional[Path] = None,
    project_override: Optional[str] = None,
) -> JiraConfig:
    """Read `.env` next to scripts/ (or given path) and build config."""
    path = env_path or DEFAULT_ENV_PATH
    if path.exists():
        load_dotenv(path)
    else:
        load_dotenv()

    config = JiraConfig(
        url=os.getenv("JIRA_URL", DEFAULT_JIRA_URL).rstrip("/"),
        project=(project_override or os.getenv("JIRA_PROJECT", DEFAULT_PROJECT)).strip(),
        pat=os.getenv("JIRA_PAT", "").strip(),
        username=os.getenv("JIRA_USERNAME", "").strip(),
        password=os.getenv("JIRA_PASSWORD", "").strip(),
        board_id=os.getenv("JIRA_BOARD_ID", "").strip(),
    )

    if not config.has_auth:
        raise JiraConfigError(
            "Нужен JIRA_PAT или пара JIRA_USERNAME + JIRA_PASSWORD в .env"
        )
    if not config.project:
        raise JiraConfigError("Не задан JIRA_PROJECT")

    return config
