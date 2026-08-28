"""Create / update / link / comment Jira issues with preview-first workflow."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from config.project_config import load_project_config, resolve_alias
from jira_client import JiraClient
from jira_client.exceptions import JiraApiError, JiraConfigError
from services import metadata as meta
from services import sprints as sprints_service

SYSTEM_REQUIRED = {"project", "issuetype", "summary"}


def build_create_preview(
    client: JiraClient,
    payload: Dict[str, Any],
    *,
    project_key: Optional[str] = None,
    board_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate payload against createmeta and return a preview plan (no writes)."""
    project = (
        payload.get("project")
        or project_key
        or (payload.get("fields") or {}).get("project")
    )
    if not project:
        raise JiraConfigError("Не указан project")

    project_config = load_project_config(str(project))
    issue_types = meta.list_create_issue_types(client, str(project))
    issue_type_name = _resolve_issue_type_name(
        payload.get("issue_type") or payload.get("issuetype"),
        project_config,
    )
    if not issue_type_name:
        raise JiraConfigError("Не указан issue_type")

    issue_type = meta.find_issue_type(issue_types, issue_type_name)
    if not issue_type:
        available = ", ".join(t.get("name") or "?" for t in issue_types)
        raise JiraConfigError(
            f"Тип задачи {issue_type_name!r} недоступен в {project}. "
            f"Доступны: {available}"
        )

    create_fields = meta.get_create_fields(
        client, str(project), str(issue_type["id"])
    )
    field_meta = meta.field_map_by_id(create_fields)

    fields, warnings, unresolved = _build_fields_for_create(
        client,
        payload,
        project_key=str(project),
        issue_type=issue_type,
        field_meta=field_meta,
        project_config=project_config,
        board_id=board_id,
    )

    missing_required = _missing_required_fields(field_meta, fields, project_config)
    links = _normalize_links(payload.get("links") or [], project_config)
    post_actions = _extract_post_actions(payload)

    browse_base = project_config.get("browse_url") or f"{client.base_url}/browse"

    return {
        "operation": "create",
        "mode": "preview",
        "project": str(project),
        "issue_type": issue_type.get("name"),
        "issue_type_id": str(issue_type.get("id")),
        "fields": fields,
        "links": links,
        "post_actions": post_actions,
        "missing_required_fields": missing_required,
        "warnings": warnings,
        "unresolved": unresolved,
        "browse_base": browse_base,
        "ready": not missing_required and not unresolved,
    }


def apply_create(
    client: JiraClient,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Create one issue from an approved preview. Then sprint + links."""
    if preview.get("missing_required_fields"):
        raise JiraConfigError(
            "Нельзя создать задачу: не заполнены обязательные поля: "
            + ", ".join(
                f.get("name") or f.get("id")
                for f in preview["missing_required_fields"]
            )
        )
    if preview.get("unresolved"):
        raise JiraConfigError(
            "Нельзя создать задачу: неоднозначные значения: "
            + ", ".join(preview["unresolved"])
        )

    body = {"fields": preview["fields"]}
    created = client.post("/rest/api/2/issue", json_body=body)
    key = created.get("key")
    result: Dict[str, Any] = {
        "operation": "create",
        "mode": "applied",
        "ok": True,
        "key": key,
        "id": created.get("id"),
        "url": f"{preview.get('browse_base')}/{key}",
        "fields": preview["fields"],
        "links": [],
        "post_actions": [],
        "errors": [],
    }

    for action in preview.get("post_actions") or []:
        try:
            applied = _apply_post_action(client, key, action)
            result["post_actions"].append({"action": action, "ok": True, **applied})
        except Exception as exc:  # noqa: BLE001 — collect partial failures
            result["post_actions"].append(
                {"action": action, "ok": False, "error": str(exc)}
            )
            result["errors"].append(f"post_action failed: {exc}")

    for link in preview.get("links") or []:
        try:
            link_result = create_link(
                client,
                source_key=key,
                target_key=link["target"],
                relation=link["relation"],
                direction=link.get("direction", "outward"),
                project_config=load_project_config(preview["project"]),
                apply=True,
            )
            result["links"].append(link_result)
        except Exception as exc:  # noqa: BLE001
            result["links"].append(
                {
                    "ok": False,
                    "source": key,
                    "target": link.get("target"),
                    "relation": link.get("relation"),
                    "error": str(exc),
                }
            )
            result["errors"].append(f"link failed: {exc}")

    if result["errors"]:
        result["ok"] = False
    return result


def build_batch_create_preview(
    client: JiraClient,
    payload: Dict[str, Any],
    *,
    project_key: Optional[str] = None,
    board_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Preview for a list of issues + cross-links (meeting outcome)."""
    issues_in = payload.get("issues") or []
    if not isinstance(issues_in, list) or not issues_in:
        raise JiraConfigError("Ожидается payload.issues — непустой список")

    previews = []
    for index, item in enumerate(issues_in):
        item_payload = dict(item)
        if project_key and not item_payload.get("project"):
            item_payload["project"] = project_key
        local_id = item_payload.pop("local_id", f"issue_{index + 1}")
        preview = build_create_preview(
            client, item_payload, project_key=project_key, board_id=board_id
        )
        preview["local_id"] = local_id
        previews.append(preview)

    links = []
    for link in payload.get("links") or []:
        links.append(
            {
                "from": link.get("from") or link.get("source"),
                "to": link.get("to") or link.get("target"),
                "relation": link.get("relation") or link.get("type") or "relates",
                "direction": link.get("direction", "outward"),
            }
        )

    ready = all(p.get("ready") for p in previews)
    return {
        "operation": "batch_create",
        "mode": "preview",
        "issues": previews,
        "links": links,
        "ready": ready,
        "warnings": [
            w for p in previews for w in (p.get("warnings") or [])
        ],
        "missing_required_fields": [
            {"local_id": p["local_id"], "fields": p["missing_required_fields"]}
            for p in previews
            if p.get("missing_required_fields")
        ],
    }


def apply_batch_create(
    client: JiraClient,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Create all issues first, then establish links (local_id or key refs)."""
    created: Dict[str, Dict[str, Any]] = {}
    failed: List[Dict[str, Any]] = []

    for item in preview.get("issues") or []:
        local_id = item.get("local_id")
        try:
            result = apply_create(client, item)
            created[local_id] = result
        except Exception as exc:  # noqa: BLE001
            failed.append({"local_id": local_id, "error": str(exc), "preview": item})

    link_results = []
    unresolved_links = []
    for link in preview.get("links") or []:
        source = _resolve_batch_key(link.get("from"), created)
        target = _resolve_batch_key(link.get("to"), created)
        if not source or not target:
            unresolved_links.append({**link, "error": "source/target not created"})
            continue
        try:
            project = next(iter(created.values())).get("fields", {}).get("project", {})
            project_key = project.get("key") if isinstance(project, dict) else None
            link_results.append(
                create_link(
                    client,
                    source_key=source,
                    target_key=target,
                    relation=link.get("relation") or "relates",
                    direction=link.get("direction", "outward"),
                    project_config=load_project_config(project_key or "CAT2"),
                    apply=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            unresolved_links.append({**link, "error": str(exc)})

    return {
        "operation": "batch_create",
        "mode": "applied",
        "created": created,
        "failed": failed,
        "links": link_results,
        "unresolved_links": unresolved_links,
        "ok": not failed and not unresolved_links,
    }


def build_update_preview(
    client: JiraClient,
    issue_key: str,
    changes: Dict[str, Any],
    *,
    project_key: Optional[str] = None,
    board_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Diff current issue vs requested changes; no writes."""
    current = client.get(
        f"/rest/api/2/issue/{issue_key}",
        params={
            "fields": "*navigable,comment",
            "expand": "names",
        },
    )
    fields_now = current.get("fields") or {}
    project = project_key or (fields_now.get("project") or {}).get("key")
    project_config = load_project_config(str(project or "CAT2"))
    edit_meta = meta.get_edit_meta(client, issue_key)
    transitions = meta.get_transitions(client, issue_key)

    field_updates, warnings, unresolved, post_actions, transition = (
        _build_fields_for_update(
            client,
            changes,
            fields_now=fields_now,
            edit_meta=edit_meta,
            transitions=transitions,
            project_config=project_config,
            project_key=str(project),
            board_id=board_id,
        )
    )

    diff = _build_diff(fields_now, field_updates, changes, transition)

    return {
        "operation": "update",
        "mode": "preview",
        "issue_key": issue_key,
        "url": f"{client.base_url}/browse/{issue_key}",
        "fields": field_updates,
        "diff": diff,
        "transition": transition,
        "post_actions": post_actions,
        "links_add": _normalize_links(changes.get("links_add") or [], project_config),
        "links_remove": changes.get("links_remove") or [],
        "comment": changes.get("comment"),
        "warnings": warnings,
        "unresolved": unresolved,
        "available_transitions": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "to": (t.get("to") or {}).get("name"),
            }
            for t in transitions
        ],
        "ready": not unresolved,
    }


def apply_update(
    client: JiraClient,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply an approved update preview."""
    if preview.get("unresolved"):
        raise JiraConfigError(
            "Нельзя обновить задачу: неоднозначные значения: "
            + ", ".join(preview["unresolved"])
        )

    key = preview["issue_key"]
    errors: List[str] = []
    applied: Dict[str, Any] = {
        "operation": "update",
        "mode": "applied",
        "issue_key": key,
        "url": preview.get("url"),
        "fields": preview.get("fields") or {},
        "transition": None,
        "comment": None,
        "links": [],
        "post_actions": [],
        "errors": errors,
        "ok": True,
    }

    if preview.get("fields"):
        try:
            client.put(
                f"/rest/api/2/issue/{key}",
                json_body={"fields": preview["fields"]},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fields update failed: {exc}")

    transition = preview.get("transition")
    if transition:
        try:
            client.post(
                f"/rest/api/2/issue/{key}/transitions",
                json_body={"transition": {"id": transition["id"]}},
            )
            applied["transition"] = transition
        except Exception as exc:  # noqa: BLE001
            errors.append(f"transition failed: {exc}")

    comment = preview.get("comment")
    if comment:
        try:
            applied["comment"] = add_comment(client, key, comment, apply=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"comment failed: {exc}")

    for action in preview.get("post_actions") or []:
        try:
            result = _apply_post_action(client, key, action)
            applied["post_actions"].append({"action": action, "ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            applied["post_actions"].append(
                {"action": action, "ok": False, "error": str(exc)}
            )
            errors.append(f"post_action failed: {exc}")

    for link in preview.get("links_add") or []:
        try:
            applied["links"].append(
                create_link(
                    client,
                    source_key=key,
                    target_key=link["target"],
                    relation=link["relation"],
                    direction=link.get("direction", "outward"),
                    apply=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"link add failed: {exc}")

    for link_id in preview.get("links_remove") or []:
        try:
            client.delete(f"/rest/api/2/issueLink/{link_id}")
            applied["links"].append({"ok": True, "removed_id": link_id})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"link remove failed: {exc}")

    applied["ok"] = not errors
    return applied


def create_link(
    client: JiraClient,
    *,
    source_key: str,
    target_key: str,
    relation: str,
    direction: str = "outward",
    project_config: Optional[Dict[str, Any]] = None,
    apply: bool = False,
) -> Dict[str, Any]:
    """Create issue link. Preview when apply=False."""
    config = project_config or {}
    link_types = meta.get_link_types(client)
    type_name = resolve_alias(
        relation, config.get("link_relation_aliases") or {}
    ) or relation

    link_type = None
    for item in link_types:
        if item.get("name") == type_name or str(item.get("id")) == str(relation):
            link_type = item
            break
        if str(item.get("name", "")).lower() == str(type_name).lower():
            link_type = item
            break
        if relation.lower() in {
            str(item.get("inward", "")).lower(),
            str(item.get("outward", "")).lower(),
        }:
            link_type = item
            if relation.lower() == str(item.get("inward", "")).lower():
                direction = "inward"
            break

    if not link_type:
        names = ", ".join(t.get("name") or "?" for t in link_types)
        raise JiraConfigError(
            f"Неизвестный тип связи {relation!r}. Доступны: {names}"
        )

    if direction == "inward":
        inward, outward = source_key, target_key
    else:
        outward, inward = source_key, target_key

    plan = {
        "operation": "link",
        "mode": "preview" if not apply else "applied",
        "type": link_type.get("name"),
        "outward_issue": outward,
        "inward_issue": inward,
        "direction": direction,
        "relation": relation,
    }
    if not apply:
        return plan

    client.post(
        "/rest/api/2/issueLink",
        json_body={
            "type": {"name": link_type.get("name")},
            "inwardIssue": {"key": inward},
            "outwardIssue": {"key": outward},
        },
    )
    plan["ok"] = True
    return plan


def add_comment(
    client: JiraClient,
    issue_key: str,
    text: str,
    *,
    apply: bool = False,
) -> Dict[str, Any]:
    plan = {
        "operation": "comment",
        "mode": "preview" if not apply else "applied",
        "issue_key": issue_key,
        "body": text,
    }
    if not apply:
        return plan
    created = client.post(
        f"/rest/api/2/issue/{issue_key}/comment",
        json_body={"body": text},
    )
    plan["id"] = created.get("id")
    plan["ok"] = True
    return plan


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------


def _resolve_issue_type_name(
    value: Optional[str],
    project_config: Dict[str, Any],
) -> Optional[str]:
    if not value:
        return None
    return resolve_alias(value, project_config.get("issue_type_aliases") or {})


def _build_fields_for_create(
    client: JiraClient,
    payload: Dict[str, Any],
    *,
    project_key: str,
    issue_type: Dict[str, Any],
    field_meta: Dict[str, Dict[str, Any]],
    project_config: Dict[str, Any],
    board_id: Optional[str],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    warnings: List[str] = []
    unresolved: List[str] = []
    fields: Dict[str, Any] = {
        "project": {"key": project_key},
        "issuetype": {"id": str(issue_type["id"])},
    }

    summary = payload.get("summary") or payload.get("title")
    if summary:
        fields["summary"] = str(summary).strip()

    description = payload.get("description")
    if description is not None:
        fields["description"] = str(description)

    logical = dict(payload.get("fields") or {})
    # Promote top-level convenience keys into logical fields.
    for key in (
        "budget",
        "severity",
        "env",
        "platform",
        "epic",
        "parent",
        "estimate_hours",
        "estimate_kind",
    ):
        if key in payload and key not in logical:
            logical[key] = payload[key]

    mapping = project_config.get("fields") or {}
    defaults = project_config.get("defaults") or {}

    # Apply configured defaults only for keys present in defaults and missing in payload.
    for logical_name, default_value in defaults.items():
        if logical_name not in logical and logical.get(logical_name) is None:
            # only if corresponding jira field is required / has default
            jira_id = mapping.get(logical_name)
            meta_field = field_meta.get(jira_id) if jira_id else None
            if meta_field and (
                meta_field.get("required") or meta_field.get("hasDefaultValue")
            ):
                logical[logical_name] = default_value
                warnings.append(
                    f"Применён default проекта для {logical_name}={default_value!r}"
                )

    # Assignee
    assignee = payload.get("assignee")
    if assignee:
        username, warn, unresolved_msg = _resolve_assignee(
            client, assignee, project_config
        )
        if warn:
            warnings.append(warn)
        if unresolved_msg:
            unresolved.append(unresolved_msg)
        elif username:
            fields["assignee"] = {"name": username}

    # Priority
    priority = payload.get("priority")
    if priority:
        resolved = resolve_alias(
            priority, project_config.get("priority_aliases") or {}
        )
        fields["priority"] = {"name": resolved}

    # Labels
    labels = payload.get("labels")
    if labels is not None:
        fields["labels"] = list(labels)

    # Components
    components = payload.get("components")
    if components:
        resolved_components = []
        for item in components:
            name = resolve_alias(
                item, project_config.get("component_aliases") or {}
            )
            resolved_components.append({"name": name})
        fields["components"] = resolved_components

    # fixVersions
    fix_versions = payload.get("fix_versions") or payload.get("fixVersions")
    if fix_versions:
        fields["fixVersions"] = [{"name": v} for v in fix_versions]

    # parent (sub-task)
    parent = payload.get("parent") or logical.pop("parent", None)
    if parent:
        fields["parent"] = {"key": str(parent)}

    # epic
    epic = payload.get("epic") or logical.pop("epic", None)
    epic_field = mapping.get("epic")
    if epic and epic_field:
        if epic_field in field_meta:
            fields[epic_field] = str(epic)
        else:
            warnings.append(
                f"Поле epic ({epic_field}) недоступно на экране создания"
            )

    # estimate via timetracking
    estimate_hours = payload.get("estimate_hours")
    if estimate_hours is None:
        estimate_hours = logical.pop("estimate_hours", None)
    if estimate_hours is not None:
        hours = _to_hours(estimate_hours)
        if hours is None:
            unresolved.append(f"estimate_hours={estimate_hours!r}")
        else:
            estimate_field = mapping.get("estimate_hours", "timetracking")
            if estimate_field == "timetracking" or estimate_field in field_meta:
                fields["timetracking"] = {
                    "originalEstimate": _hours_to_jira(hours),
                }
            else:
                # try custom numeric field
                kind = payload.get("estimate_kind") or logical.pop(
                    "estimate_kind", "common"
                )
                custom = mapping.get(f"estimate_{kind}") or mapping.get(
                    "estimate_common"
                )
                if custom and custom in field_meta:
                    fields[custom] = hours
                else:
                    warnings.append(
                        "Estimate-поле недоступно на create-экране; "
                        "значение не будет отправлено"
                    )

    # Logical / explicit custom fields
    for logical_name, value in list(logical.items()):
        if value is None:
            continue
        jira_id = mapping.get(logical_name, logical_name)
        if jira_id in {"timetracking"}:
            continue
        if jira_id not in field_meta and not jira_id.startswith("customfield_"):
            # maybe already a jira id missing from meta
            warnings.append(f"Поле {logical_name}/{jira_id} нет в createmeta")
            continue
        if jira_id not in field_meta:
            warnings.append(f"Поле {jira_id} ({logical_name}) недоступно при создании")
            continue
        fields[jira_id] = _coerce_field_value(
            field_meta[jira_id], value, warnings, unresolved, logical_name
        )

    # Sprint is post-create action (Agile API)
    sprint = payload.get("sprint")
    if sprint is not None:
        sprint_id, warn, unresolved_msg = _resolve_sprint_id(
            client, sprint, project_key, board_id
        )
        if warn:
            warnings.append(warn)
        if unresolved_msg:
            unresolved.append(unresolved_msg)
        # stored later via post_actions in caller through payload side channel
        payload["_resolved_sprint_id"] = sprint_id

    return fields, warnings, unresolved


def _extract_post_actions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    sprint_id = payload.get("_resolved_sprint_id")
    if sprint_id:
        actions.append({"type": "add_to_sprint", "sprint_id": sprint_id})
    payload.pop("_resolved_sprint_id", None)
    return actions


def _build_fields_for_update(
    client: JiraClient,
    changes: Dict[str, Any],
    *,
    fields_now: Dict[str, Any],
    edit_meta: Dict[str, Any],
    transitions: List[Dict[str, Any]],
    project_config: Dict[str, Any],
    project_key: str,
    board_id: Optional[str],
) -> Tuple[
    Dict[str, Any],
    List[str],
    List[str],
    List[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    warnings: List[str] = []
    unresolved: List[str] = []
    field_updates: Dict[str, Any] = {}
    post_actions: List[Dict[str, Any]] = []
    mapping = project_config.get("fields") or {}

    if "summary" in changes or "title" in changes:
        field_updates["summary"] = str(
            changes.get("summary") or changes.get("title")
        ).strip()

    if "description" in changes:
        field_updates["description"] = changes.get("description")

    if "assignee" in changes:
        assignee = changes.get("assignee")
        if assignee in (None, "", "unassigned"):
            field_updates["assignee"] = None
        else:
            username, warn, unresolved_msg = _resolve_assignee(
                client, assignee, project_config
            )
            if warn:
                warnings.append(warn)
            if unresolved_msg:
                unresolved.append(unresolved_msg)
            elif username:
                field_updates["assignee"] = {"name": username}

    if "priority" in changes:
        resolved = resolve_alias(
            changes.get("priority"),
            project_config.get("priority_aliases") or {},
        )
        field_updates["priority"] = {"name": resolved}

    if "labels" in changes:
        field_updates["labels"] = list(changes["labels"])
    else:
        labels_add = changes.get("labels_add") or []
        labels_remove = set(changes.get("labels_remove") or [])
        if labels_add or labels_remove:
            current = list(fields_now.get("labels") or [])
            merged = [x for x in current if x not in labels_remove]
            for label in labels_add:
                if label not in merged:
                    merged.append(label)
            field_updates["labels"] = merged

    if "components" in changes:
        field_updates["components"] = [
            {
                "name": resolve_alias(
                    c, project_config.get("component_aliases") or {}
                )
            }
            for c in changes["components"]
        ]

    if "fix_versions" in changes or "fixVersions" in changes:
        versions = changes.get("fix_versions") or changes.get("fixVersions")
        field_updates["fixVersions"] = [{"name": v} for v in versions]

    if "epic" in changes:
        epic_field = mapping.get("epic")
        if epic_field and epic_field in edit_meta:
            value = changes.get("epic")
            field_updates[epic_field] = value
        else:
            warnings.append("Epic Link недоступен для редактирования")

    if "parent" in changes:
        if "parent" in edit_meta:
            field_updates["parent"] = {"key": changes["parent"]}
        else:
            warnings.append("parent недоступен для редактирования через editmeta")

    if "estimate_hours" in changes:
        hours = _to_hours(changes.get("estimate_hours"))
        if hours is None:
            unresolved.append(f"estimate_hours={changes.get('estimate_hours')!r}")
        elif "timetracking" in edit_meta:
            field_updates["timetracking"] = {
                "originalEstimate": _hours_to_jira(hours)
            }
        else:
            warnings.append("timetracking недоступен в editmeta")

    # Logical custom fields
    logical = dict(changes.get("fields") or {})
    for key in ("budget", "severity", "env", "platform"):
        if key in changes and key not in logical:
            logical[key] = changes[key]
    for logical_name, value in logical.items():
        jira_id = mapping.get(logical_name, logical_name)
        if jira_id not in edit_meta:
            warnings.append(f"Поле {jira_id} ({logical_name}) недоступно в editmeta")
            continue
        field_updates[jira_id] = _coerce_field_value(
            edit_meta[jira_id], value, warnings, unresolved, logical_name
        )

    if "sprint" in changes:
        sprint_id, warn, unresolved_msg = _resolve_sprint_id(
            client, changes.get("sprint"), project_key, board_id
        )
        if warn:
            warnings.append(warn)
        if unresolved_msg:
            unresolved.append(unresolved_msg)
        elif sprint_id:
            post_actions.append({"type": "add_to_sprint", "sprint_id": sprint_id})

    transition = None
    status = changes.get("status") or changes.get("transition")
    if status:
        transition = _find_transition(transitions, str(status))
        if not transition:
            names = ", ".join(
                f"{t.get('name')}→{(t.get('to') or {}).get('name')}"
                for t in transitions
            )
            unresolved.append(
                f"Нет перехода {status!r}. Доступны: {names or '—'}"
            )

    # Drop fields not present in editmeta (except we already checked most)
    for field_id in list(field_updates.keys()):
        if field_id not in edit_meta and field_id != "timetracking":
            # timetracking checked above
            if field_id not in edit_meta:
                warnings.append(
                    f"Поле {field_id} отсутствует в editmeta — пропущено"
                )
                field_updates.pop(field_id, None)

    return field_updates, warnings, unresolved, post_actions, transition


def _missing_required_fields(
    field_meta: Dict[str, Dict[str, Any]],
    fields: Dict[str, Any],
    project_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    missing = []
    reverse_map = {
        jira_id: logical
        for logical, jira_id in (project_config.get("fields") or {}).items()
    }
    for field_id, meta_field in field_meta.items():
        if not meta_field.get("required"):
            continue
        if field_id in SYSTEM_REQUIRED:
            # summary checked separately
            if field_id == "summary" and not fields.get("summary"):
                missing.append(
                    {
                        "id": field_id,
                        "name": meta_field.get("name"),
                        "logical": "summary",
                        "allowed_values": meta.compact_allowed(
                            meta_field.get("allowedValues")
                        ),
                    }
                )
            continue
        if field_id not in fields or fields.get(field_id) in (None, "", []):
            # if hasDefaultValue and we didn't set it, Jira may accept omit —
            # but for required without our value, still list unless default applied
            if meta_field.get("hasDefaultValue") and field_id not in fields:
                # safe to omit — Jira applies default
                continue
            missing.append(
                {
                    "id": field_id,
                    "name": meta_field.get("name"),
                    "logical": reverse_map.get(field_id),
                    "allowed_values": meta.compact_allowed(
                        meta_field.get("allowedValues")
                    ),
                }
            )
    return missing


def _coerce_field_value(
    field_meta: Dict[str, Any],
    value: Any,
    warnings: List[str],
    unresolved: List[str],
    logical_name: str,
) -> Any:
    schema = field_meta.get("schema") or {}
    field_type = schema.get("type")
    allowed = field_meta.get("allowedValues") or []

    if field_type == "option":
        match = _match_option(value, allowed)
        if match is None:
            names = [
                a.get("value") or a.get("name")
                for a in allowed
                if isinstance(a, dict)
            ]
            unresolved.append(
                f"{logical_name}={value!r} не из allowed: {names}"
            )
            return value
        return {"id": match.get("id")} if match.get("id") else {"value": match.get("value")}

    if field_type == "array" and schema.get("items") == "string":
        if isinstance(value, list):
            return value
        return [value]

    if field_type == "array" and schema.get("items") == "component":
        if isinstance(value, list):
            return [{"name": v} if isinstance(v, str) else v for v in value]
        return [{"name": value}]

    if field_type == "number":
        return _to_hours(value) if isinstance(value, str) else value

    if field_type == "user":
        if isinstance(value, dict):
            return value
        return {"name": value}

    if field_type == "issuelink":
        if isinstance(value, dict):
            return value
        return {"key": value}

    return value


def _match_option(value: Any, allowed: List[Any]) -> Optional[Dict[str, Any]]:
    text = str(value).strip().lower()
    for item in allowed:
        if not isinstance(item, dict):
            continue
        candidates = [
            str(item.get("value") or "").lower(),
            str(item.get("name") or "").lower(),
            str(item.get("id") or "").lower(),
        ]
        if text in candidates:
            return item
    return None


def _resolve_assignee(
    client: JiraClient,
    value: Any,
    project_config: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    raw = str(value).strip()
    aliased = resolve_alias(raw, project_config.get("people_aliases") or {})
    # Exact username
    users = meta.search_users(client, aliased, max_results=10)
    exact = [u for u in users if u.get("name") == aliased]
    if exact:
        return exact[0].get("name"), None, None
    if len(users) == 1:
        return users[0].get("name"), f"assignee: {raw} → {users[0].get('name')}", None
    if not users:
        # try original raw
        if aliased != raw:
            users = meta.search_users(client, raw, max_results=10)
        if len(users) == 1:
            return users[0].get("name"), None, None
        if not users:
            return None, None, f"assignee не найден: {raw}"
    options = ", ".join(
        f"{u.get('name')} ({u.get('displayName')})" for u in users[:5]
    )
    return None, None, f"assignee неоднозначен: {raw} → {options}"


def _resolve_sprint_id(
    client: JiraClient,
    value: Any,
    project_key: str,
    board_id: Optional[str],
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    if value is None:
        return None, None, None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return int(value), None, None

    text = str(value).strip().lower()
    try:
        board = sprints_service.find_board_id(client, project_key, board_id)
    except JiraApiError as exc:
        return None, None, f"sprint: не удалось найти доску ({exc})"

    if text in {"active", "текущий", "current"}:
        sprint = sprints_service.get_active_sprint(client, board)
        return int(sprint["id"]), f"sprint → active {sprint.get('name')}", None

    # search active+future
    data = client.get(
        f"/rest/agile/1.0/board/{board}/sprint",
        params={"state": "active,future", "maxResults": 50},
    )
    sprints = data.get("values") or []
    matches = [
        s
        for s in sprints
        if text in str(s.get("name", "")).lower()
        or str(s.get("id")) == text
    ]
    if len(matches) == 1:
        return int(matches[0]["id"]), None, None
    if not matches:
        return None, None, f"sprint не найден: {value}"
    names = ", ".join(f"{s.get('id')}:{s.get('name')}" for s in matches)
    return None, None, f"sprint неоднозначен: {names}"


def _find_transition(
    transitions: List[Dict[str, Any]],
    status: str,
) -> Optional[Dict[str, Any]]:
    needle = status.strip().lower()
    for item in transitions:
        name = str(item.get("name") or "").lower()
        to_name = str((item.get("to") or {}).get("name") or "").lower()
        if needle in {name, to_name, str(item.get("id"))}:
            return {
                "id": item.get("id"),
                "name": item.get("name"),
                "to": (item.get("to") or {}).get("name"),
            }
    return None


def _normalize_links(
    links: List[Dict[str, Any]],
    project_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    result = []
    for link in links:
        relation = link.get("relation") or link.get("type") or "relates"
        relation = resolve_alias(
            relation, project_config.get("link_relation_aliases") or {}
        )
        result.append(
            {
                "target": link.get("target") or link.get("to") or link.get("key"),
                "relation": relation,
                "direction": link.get("direction", "outward"),
            }
        )
    return result


def _apply_post_action(
    client: JiraClient,
    issue_key: str,
    action: Dict[str, Any],
) -> Dict[str, Any]:
    if action.get("type") == "add_to_sprint":
        sprint_id = action["sprint_id"]
        client.post(
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            json_body={"issues": [issue_key]},
        )
        return {"sprint_id": sprint_id}
    raise JiraConfigError(f"Неизвестное post_action: {action}")


def _build_diff(
    fields_now: Dict[str, Any],
    field_updates: Dict[str, Any],
    changes: Dict[str, Any],
    transition: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    diff: List[Dict[str, Any]] = []

    def _disp(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return (
                value.get("displayName")
                or value.get("name")
                or value.get("value")
                or value.get("key")
                or value
            )
        if isinstance(value, list):
            return [_disp(v) for v in value]
        return value

    mapping = {
        "summary": "Summary",
        "description": "Description",
        "assignee": "Assignee",
        "priority": "Priority",
        "labels": "Labels",
        "components": "Components",
        "fixVersions": "Fix Version",
        "timetracking": "Estimate",
        "parent": "Parent",
    }

    for field_id, new_value in field_updates.items():
        old_value = fields_now.get(field_id)
        label = mapping.get(field_id, field_id)
        if field_id == "timetracking":
            old_disp = (old_value or {}).get("originalEstimate")
            new_disp = (new_value or {}).get("originalEstimate")
        elif field_id == "assignee":
            old_disp = _disp(old_value)
            new_disp = _disp(new_value) if new_value else None
        elif field_id in {"components", "fixVersions"}:
            old_disp = [c.get("name") for c in (old_value or [])]
            new_disp = [c.get("name") if isinstance(c, dict) else c for c in (new_value or [])]
        elif field_id == "priority":
            old_disp = (old_value or {}).get("name")
            new_disp = (new_value or {}).get("name")
        else:
            old_disp = _disp(old_value)
            new_disp = _disp(new_value)
        diff.append({"field": label, "from": old_disp, "to": new_disp})

    if "labels_add" in changes or "labels_remove" in changes:
        # already reflected in labels update; skip duplicate
        pass

    if transition:
        diff.append(
            {
                "field": "Status",
                "from": (fields_now.get("status") or {}).get("name"),
                "to": transition.get("to") or transition.get("name"),
            }
        )

    if changes.get("comment"):
        diff.append({"field": "Comment", "from": None, "to": changes["comment"]})

    return diff


def _resolve_batch_key(
    ref: Optional[str],
    created: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    if not ref:
        return None
    if ref in created:
        return created[ref].get("key")
    # already a Jira key
    if re.match(r"^[A-Z][A-Z0-9]+-\d+$", ref):
        return ref
    return None


def _to_hours(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", ".")
    text = text.replace("часов", "").replace("часа", "").replace("час", "")
    text = text.replace("hours", "").replace("hour", "").replace("h", "")
    text = text.replace("ч", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _hours_to_jira(hours: float) -> str:
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours}h"
