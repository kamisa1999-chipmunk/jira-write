"""Resolve the .env location, including an external file for plugin installs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

EXTERNAL_ENV_VAR = "JIRA_WRITE_ENV_FILE"


def resolve_env_path(default_env_path: Path, env_path: Optional[Path] = None) -> Optional[Path]:
    """Explicit arg wins, then JIRA_WRITE_ENV_FILE, then the default .env.

    Returns None when no candidate file exists, so callers fall back to
    plain load_dotenv() (process environment).
    """
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    external = (os.getenv(EXTERNAL_ENV_VAR) or "").strip()
    if external:
        candidates.append(Path(external).expanduser())
    candidates.append(Path(default_env_path))
    for path in candidates:
        if path.is_file():
            return path
    return None
