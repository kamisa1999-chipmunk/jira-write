"""Sprint payload helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional


def normalize_sprint(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the fields used in sprint snapshot reports."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "state": raw.get("state"),
        "startDate": raw.get("startDate"),
        "endDate": raw.get("endDate"),
        "goal": raw.get("goal"),
    }


def sprint_date_fragment(iso_value: Optional[str]) -> str:
    """YYYY-MM-DD from Jira datetime, or 'unknown'."""
    if not iso_value:
        return "unknown"
    return iso_value[:10]
