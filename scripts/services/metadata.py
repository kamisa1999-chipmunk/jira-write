"""Jira metadata helpers for create/edit screens, transitions, links, users."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from jira_client import JiraClient


def list_create_issue_types(client: JiraClient, project_key: str) -> List[Dict[str, Any]]:
    """Paginated createmeta issue types for a project."""
    types: List[Dict[str, Any]] = []
    start_at = 0
    while True:
        data = client.get(
            f"/rest/api/2/issue/createmeta/{project_key}/issuetypes",
            params={"startAt": start_at, "maxResults": 50},
        )
        batch = data.get("values") or []
        types.extend(batch)
        if data.get("isLast") or not batch:
            break
        start_at += len(batch)
    return types


def get_create_fields(
    client: JiraClient,
    project_key: str,
    issue_type_id: str,
) -> List[Dict[str, Any]]:
    """Field metadata for create screen of a given issue type."""
    fields: List[Dict[str, Any]] = []
    start_at = 0
    while True:
        data = client.get(
            f"/rest/api/2/issue/createmeta/{project_key}/issuetypes/{issue_type_id}",
            params={"startAt": start_at, "maxResults": 100},
        )
        batch = data.get("values") or []
        fields.extend(batch)
        if data.get("isLast") or not batch:
            break
        start_at += len(batch)
    return fields


def get_edit_meta(client: JiraClient, issue_key: str) -> Dict[str, Any]:
    data = client.get(f"/rest/api/2/issue/{issue_key}/editmeta")
    return data.get("fields") or {}


def get_transitions(client: JiraClient, issue_key: str) -> List[Dict[str, Any]]:
    data = client.get(f"/rest/api/2/issue/{issue_key}/transitions")
    return data.get("transitions") or []


def get_priorities(client: JiraClient) -> List[Dict[str, Any]]:
    return client.get("/rest/api/2/priority") or []


def get_link_types(client: JiraClient) -> List[Dict[str, Any]]:
    data = client.get("/rest/api/2/issueLinkType")
    return data.get("issueLinkTypes") or []


def get_components(client: JiraClient, project_key: str) -> List[Dict[str, Any]]:
    return client.get(f"/rest/api/2/project/{project_key}/components") or []


def get_versions(client: JiraClient, project_key: str) -> List[Dict[str, Any]]:
    return client.get(f"/rest/api/2/project/{project_key}/versions") or []


def search_users(
    client: JiraClient,
    query: str,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Search users by username/display name fragment."""
    return (
        client.get(
            "/rest/api/2/user/search",
            params={"username": query, "maxResults": max_results},
        )
        or []
    )


def resolve_user(
    client: JiraClient,
    query: str,
    *,
    people_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Resolve free-text name / alias / username to a single Jira user.

    Returns:
      {"ok": True, "user": {...}, "warning": optional str}
      {"ok": False, "error": str, "candidates": [...]}
    """
    from config.project_config import resolve_alias

    raw = (query or "").strip()
    if not raw:
        return {"ok": False, "error": "Пустое имя сотрудника", "candidates": []}

    aliased = resolve_alias(raw, people_aliases or {}) or raw
    users = search_users(client, aliased, max_results=10)
    exact = [u for u in users if u.get("name") == aliased]
    if exact:
        return {"ok": True, "user": exact[0], "warning": None}

    if len(users) == 1:
        warning = None
        if aliased != raw or users[0].get("name") != raw:
            warning = f"{raw} → {users[0].get('name')} ({users[0].get('displayName')})"
        return {"ok": True, "user": users[0], "warning": warning}

    if not users and aliased != raw:
        users = search_users(client, raw, max_results=10)
        if len(users) == 1:
            return {"ok": True, "user": users[0], "warning": None}

    if not users:
        return {"ok": False, "error": f"Сотрудник не найден в Jira: {raw}", "candidates": []}

    candidates = [
        {
            "name": u.get("name"),
            "display_name": u.get("displayName"),
            "email": u.get("emailAddress"),
        }
        for u in users[:8]
    ]
    return {
        "ok": False,
        "error": f"Сотрудник неоднозначен: {raw}",
        "candidates": candidates,
    }


def find_issue_type(
    issue_types: List[Dict[str, Any]],
    name_or_id: str,
) -> Optional[Dict[str, Any]]:
    needle = (name_or_id or "").strip()
    if not needle:
        return None
    for item in issue_types:
        if str(item.get("id")) == needle or item.get("name") == needle:
            return item
    needle_l = needle.lower()
    for item in issue_types:
        if str(item.get("name", "")).lower() == needle_l:
            return item
    return None


def field_map_by_id(fields: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for field in fields:
        field_id = field.get("fieldId") or field.get("key") or field.get("id")
        if field_id:
            result[str(field_id)] = field
    return result


def summarize_create_metadata(
    project_key: str,
    issue_types: List[Dict[str, Any]],
    fields_by_type: Dict[str, List[Dict[str, Any]]],
    priorities: List[Dict[str, Any]],
    components: List[Dict[str, Any]],
    versions: List[Dict[str, Any]],
    link_types: List[Dict[str, Any]],
    project_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compact metadata snapshot for agents / CLI."""
    types_out = []
    for issue_type in issue_types:
        type_id = str(issue_type.get("id"))
        fields = fields_by_type.get(type_id) or []
        required = []
        optional_named = []
        for field in fields:
            field_id = field.get("fieldId")
            entry = {
                "id": field_id,
                "name": field.get("name"),
                "required": bool(field.get("required")),
                "type": (field.get("schema") or {}).get("type"),
                "has_default": bool(field.get("hasDefaultValue")),
                "default": _compact_default(field.get("defaultValue")),
                "allowed_values": compact_allowed(field.get("allowedValues")),
                "operations": field.get("operations") or [],
            }
            if field.get("required") and field_id not in {
                "project",
                "issuetype",
                "summary",
            }:
                required.append(entry)
            if field_id in {
                "assignee",
                "priority",
                "labels",
                "components",
                "fixVersions",
                "description",
                "parent",
                "timetracking",
            } or (field_id or "").startswith("customfield_"):
                optional_named.append(entry)

        types_out.append(
            {
                "id": type_id,
                "name": issue_type.get("name"),
                "subtask": bool(issue_type.get("subtask")),
                "required_fields": required,
                "fields": optional_named,
            }
        )

    return {
        "project": project_key,
        "issue_types": types_out,
        "priorities": [
            {"id": p.get("id"), "name": p.get("name")} for p in priorities
        ],
        "components": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "archived": bool(c.get("archived")),
            }
            for c in components
            if not c.get("archived") and not c.get("deleted")
        ],
        "versions": [
            {"id": v.get("id"), "name": v.get("name"), "released": v.get("released")}
            for v in versions
        ],
        "link_types": [
            {
                "id": lt.get("id"),
                "name": lt.get("name"),
                "inward": lt.get("inward"),
                "outward": lt.get("outward"),
            }
            for lt in link_types
        ],
        "field_mapping": (project_config or {}).get("fields") or {},
        "defaults": (project_config or {}).get("defaults") or {},
        "config_notes": (project_config or {}).get("notes") or [],
        "config_missing": bool((project_config or {}).get("_missing")),
    }


def _compact_default(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            k: value.get(k)
            for k in ("id", "name", "value", "key")
            if k in value
        } or value
    return value


def compact_allowed(values: Optional[List[Any]]) -> List[Any]:
    if not values:
        return []
    compact: List[Any] = []
    for item in values:
        if isinstance(item, dict):
            compact.append(
                {
                    k: item.get(k)
                    for k in ("id", "name", "value", "key")
                    if k in item
                }
            )
        else:
            compact.append(item)
    return compact
