"""Load per-project field mappings and aliases (no hardcoded customfield ids in skills)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from jira_client.exceptions import JiraConfigError

CONFIG_DIR = Path(__file__).resolve().parent / "projects"


def load_project_config(project_key: str) -> Dict[str, Any]:
    """Load `config/projects/{PROJECT}.json`. Missing file → empty mapping with key only."""
    key = (project_key or "").strip().upper()
    if not key:
        raise JiraConfigError("Не задан ключ проекта для project config")

    path = CONFIG_DIR / f"{key}.json"
    if not path.exists():
        return {
            "project_key": key,
            "fields": {},
            "defaults": {},
            "issue_type_aliases": {},
            "people_aliases": {},
            "component_aliases": {},
            "priority_aliases": {},
            "link_relation_aliases": {},
            "notes": [
                f"Нет файла {path.name}: логические имена custom-полей не настроены."
            ],
            "_path": None,
            "_missing": True,
        }

    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    data.setdefault("project_key", key)
    data.setdefault("fields", {})
    data.setdefault("defaults", {})
    data.setdefault("issue_type_aliases", {})
    data.setdefault("people_aliases", {})
    data.setdefault("people_roles", {})
    data.setdefault("component_aliases", {})
    data.setdefault("priority_aliases", {})
    data.setdefault("link_relation_aliases", {})
    data.setdefault("notes", [])
    data["_path"] = str(path)
    data["_missing"] = False
    return data


def resolve_alias(
    value: Optional[str],
    aliases: Dict[str, str],
) -> Optional[str]:
    """Resolve a free-text value through alias map (case-insensitive)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    mapped = aliases.get(text.lower())
    return mapped or text
