"""Normalize Jira issue fields and compute status category / risks."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# Status → category mapping used by CAT2 workflow.
# TODO: if another project uses different status names, make this configurable.
STATUS_CATEGORY = {
    "Done": "closed",
    "Canceled": "closed",
    "To Prod": "ready_to_prod",
    "Development": "in_progress",
    "Code Review": "in_progress",
    "Testing": "in_progress",
    "В работе": "in_progress",
    "New": "not_started",
    "To Test": "testing_queue",
    "To Discovery": "returned_or_discovery",
    "Discovery": "discovery",
    "Delivery": "delivery",
    "To Launch": "to_launch",
}

CATEGORY_LABELS = {
    "closed": "Закрыто (Done)",
    "ready_to_prod": "Готово к релизу (To Prod)",
    "in_progress": "В работе (Dev / CR / Testing)",
    "not_started": "Не начато (New)",
    "testing_queue": "Очередь на тест (To Test)",
    "returned_after_testing": "Возврат после тестирования",
    "returned_or_discovery": "To Discovery",
    "discovery": "Discovery / аналитика",
    "delivery": "Delivery",
    "to_launch": "To Launch",
    "other": "Прочее",
}

STUCK_OVER_ESTIMATE_RATIO = 1.30
LONG_STATUS_HOURS = 48


def parse_jira_datetime(value: str) -> datetime:
    cleaned = value.replace("Z", "+00:00")
    if len(cleaned) >= 5 and cleaned[-5] in "+-" and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    return datetime.fromisoformat(cleaned)


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def seconds_to_hours(seconds: Optional[int]) -> Optional[float]:
    if seconds is None:
        return None
    return round(seconds / 3600.0, 2)


def _extract_text(value: Any) -> Optional[str]:
    """Convert Jira string / ADF-like content into readable plain text."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        text = "\n".join(part for part in parts if part)
        return text or None
    if isinstance(value, dict):
        node_type = value.get("type")
        content = value.get("content")

        if node_type == "text":
            return value.get("text") or None
        if node_type in {"hardBreak", "rule"}:
            return "\n"
        if node_type in {"paragraph", "blockquote"}:
            return _extract_text(content)
        if node_type in {"doc", "panel"}:
            return _extract_text(content)
        if node_type in {"bulletList", "orderedList"}:
            parts = [_extract_text(item) for item in content or []]
            text = "\n".join(part for part in parts if part)
            return text or None
        if node_type == "listItem":
            item_text = _extract_text(content)
            return f"- {item_text}" if item_text else None
        if node_type == "codeBlock":
            return _extract_text(content)
        if node_type == "mention":
            attrs = value.get("attrs") or {}
            return attrs.get("text") or attrs.get("displayName") or None
        if node_type == "emoji":
            attrs = value.get("attrs") or {}
            return attrs.get("text") or attrs.get("shortName") or None

        if "text" in value:
            return value.get("text") or None
        return _extract_text(content)

    return str(value).strip() or None


def _normalize_user(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Optional[str]]]:
    if not user:
        return None
    return {
        "account_id": user.get("accountId"),
        "display_name": user.get("displayName"),
        "email": user.get("emailAddress"),
        "name": user.get("name"),
    }


def _normalize_status(status_obj: Dict[str, Any], category: str) -> Dict[str, Optional[str]]:
    category_obj = status_obj.get("statusCategory") or {}
    return {
        "name": status_obj.get("name"),
        "category": category,
        "jira_category": category_obj.get("name"),
    }


def _option_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        return (
            value.get("value")
            or value.get("name")
            or value.get("displayName")
            or None
        )
    return str(value).strip() or None


def _normalize_platform(fields: Dict[str, Any]) -> Optional[str]:
    """CAT2 Platform (customfield_12201); fallback to first component."""
    raw = fields.get("customfield_12201")
    if isinstance(raw, list):
        names = [_option_name(item) for item in raw]
        joined = ", ".join(name for name in names if name)
        if joined:
            return joined
    else:
        name = _option_name(raw)
        if name:
            return name

    components = _normalize_components(fields)
    if components:
        return ", ".join(components)
    return None


def _normalize_components(fields: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for item in fields.get("components") or []:
        name = _option_name(item)
        if name:
            names.append(name)
    return names


def _normalize_dates(fields: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolved": fields.get("resolutiondate"),
        "duedate": fields.get("duedate"),
    }


def _pick_sprint(fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sprint_candidates: List[Dict[str, Any]] = []

    for value in fields.values():
        if isinstance(value, dict) and {"id", "name"}.issubset(value.keys()):
            if "state" in value or "startDate" in value or "endDate" in value:
                sprint_candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and {"id", "name"}.issubset(item.keys()):
                    if "state" in item or "startDate" in item or "endDate" in item:
                        sprint_candidates.append(item)

    if not sprint_candidates:
        return None

    for state in ("active", "future", "closed"):
        for sprint in sprint_candidates:
            if str(sprint.get("state", "")).lower() == state:
                return _normalize_sprint(sprint)
    return _normalize_sprint(sprint_candidates[-1])


def _normalize_sprint(sprint: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": sprint.get("id"),
        "name": sprint.get("name"),
        "state": sprint.get("state"),
        "board_id": sprint.get("boardId") or sprint.get("originBoardId"),
        "goal": sprint.get("goal"),
        "start_date": sprint.get("startDate"),
        "end_date": sprint.get("endDate"),
        "complete_date": sprint.get("completeDate"),
    }


def _normalize_comments(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    comment_block = fields.get("comment") or {}
    comments = comment_block.get("comments") or []
    normalized: List[Dict[str, Any]] = []

    for comment in comments:
        author = _normalize_user(comment.get("author"))
        normalized.append(
            {
                "id": comment.get("id"),
                "author": author,
                "body": _extract_text(comment.get("body")),
                "created": comment.get("created"),
                "updated": comment.get("updated"),
            }
        )
    return normalized


def _normalize_changelog(changelog: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not changelog:
        return []

    histories = changelog.get("histories") or []
    normalized: List[Dict[str, Any]] = []
    for history in histories:
        author = _normalize_user(history.get("author"))
        items = history.get("items") or []
        normalized_items = [
            {
                "field": item.get("field"),
                "field_type": item.get("fieldtype"),
                "from": item.get("fromString"),
                "to": item.get("toString"),
            }
            for item in items
        ]
        normalized.append(
            {
                "id": history.get("id"),
                "author": author,
                "created": history.get("created"),
                "items": normalized_items,
            }
        )
    return normalized


def _normalize_links(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []

    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        for direction in ("outwardIssue", "inwardIssue"):
            issue = link.get(direction)
            if not issue:
                continue
            issue_fields = issue.get("fields") or {}
            status_obj = issue_fields.get("status") or {}
            normalized.append(
                {
                    "direction": "outward" if direction == "outwardIssue" else "inward",
                    "type": link_type.get(
                        "outward" if direction == "outwardIssue" else "inward"
                    ),
                    "key": issue.get("key"),
                    "title": issue_fields.get("summary"),
                    "status": status_obj.get("name"),
                    "issue_type": (issue_fields.get("issuetype") or {}).get("name"),
                }
            )

    return normalized


def is_testing_task(summary: str) -> bool:
    return summary.strip().lower().startswith("testing")


def pick_estimate_hours(fields: Dict[str, Any], summary: str, status: str) -> Optional[float]:
    """Pick estimate in hours from CAT2 custom fields / timetracking.

    Priority mirrors the legacy script:
    testing* summary → QA estimate fields;
    Discovery statuses → analytics estimate;
    Development/CR → dev estimate;
    else common estimate field, then timeoriginalestimate.
    """
    if is_testing_task(summary):
        for field in ("customfield_11332", "customfield_11327"):
            value = to_float(fields.get(field))
            if value is not None:
                return value

    if status in {"Discovery", "To Discovery", "В работе"}:
        value = to_float(fields.get("customfield_11330"))
        if value is not None:
            return value

    if status in {"Development", "Code Review"}:
        value = to_float(fields.get("customfield_11331"))
        if value is not None:
            return value

    value = to_float(fields.get("customfield_10618"))
    if value is not None:
        return value

    return seconds_to_hours(fields.get("timeoriginalestimate"))


def hours_in_current_status(changelog: Dict[str, Any], current_status: str) -> Optional[float]:
    entered_at: Optional[datetime] = None
    for history in reversed(changelog.get("histories", [])):
        for item in history.get("items", []):
            if item.get("field") == "status" and item.get("toString") == current_status:
                entered_at = parse_jira_datetime(history["created"])
                break
        if entered_at:
            break

    if not entered_at:
        return None

    now = datetime.now(entered_at.tzinfo)
    return round((now - entered_at).total_seconds() / 3600.0, 2)


def was_returned_after_testing(changelog: Dict[str, Any], current_status: str) -> bool:
    """True if task left Testing/To Test back into development-like status.

    TODO: rule only looks at status names and current status filter from the
    legacy script; multi-cycle returns still count as a single boolean flag.
    """
    if current_status not in {"Development", "Code Review", "To Discovery"}:
        return False

    previous_status: Optional[str] = None
    for history in changelog.get("histories", []):
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            to_status = item.get("toString")
            if to_status in {"Testing", "To Test"}:
                previous_status = to_status
            if (
                previous_status in {"Testing", "To Test"}
                and to_status in {"Development", "Code Review", "To Discovery"}
            ):
                return True
    return False


def build_risk_flags(
    status: str,
    estimate_hours: Optional[float],
    spent_hours: Optional[float],
    hours_in_status: Optional[float],
    returned_after_testing: bool,
) -> List[str]:
    risks: List[str] = []

    if returned_after_testing:
        risks.append("returned_after_testing")

    if estimate_hours and spent_hours and spent_hours > estimate_hours * STUCK_OVER_ESTIMATE_RATIO:
        risks.append("over_estimate")

    if status in {"Code Review", "To Test"} and hours_in_status and hours_in_status > LONG_STATUS_HOURS:
        risks.append("long_in_status")

    # TODO: stuck_in_work overlaps with over_estimate (same 130% threshold);
    # kept as in legacy for status-specific wording in reports.
    if status in {"Development", "В работе", "Testing"} and estimate_hours and spent_hours:
        if spent_hours > estimate_hours * STUCK_OVER_ESTIMATE_RATIO:
            risks.append("stuck_in_work")

    return risks


def normalize_issue(
    issue: Dict[str, Any],
    jira_url: str,
    changelog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fields = issue.get("fields", {})
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    status_obj = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    priority = fields.get("priority") or {}
    status = status_obj.get("name")
    summary = fields.get("summary") or ""
    description = _extract_text(fields.get("description"))

    effective_changelog = changelog if changelog is not None else issue.get("changelog")

    estimate_hours = pick_estimate_hours(fields, summary, status or "")
    spent_hours = seconds_to_hours(fields.get("timespent"))
    timetracking = fields.get("timetracking") or {}

    hours_in_status = None
    returned_after_testing = False
    if effective_changelog is not None and status:
        hours_in_status = hours_in_current_status(effective_changelog, status)
        returned_after_testing = was_returned_after_testing(effective_changelog, status)

    category = STATUS_CATEGORY.get(status or "", "other")
    if returned_after_testing:
        category = "returned_after_testing"

    risks = build_risk_flags(
        status or "",
        estimate_hours,
        spent_hours,
        hours_in_status,
        returned_after_testing,
    )

    return {
        "key": issue.get("key"),
        "title": summary,
        "summary": summary,
        "description": description,
        "status": status,
        "status_details": _normalize_status(status_obj, category),
        "status_category": category,
        "assignee": assignee.get("displayName"),
        "assignee_details": _normalize_user(assignee),
        "author": reporter.get("displayName"),
        "author_details": _normalize_user(reporter),
        "type": issue_type.get("name"),
        "priority": priority.get("name"),
        "dates": _normalize_dates(fields),
        "is_testing_task": is_testing_task(summary),
        "estimate_hours": estimate_hours,
        "spent_hours": spent_hours,
        "estimate_display": timetracking.get("originalEstimate"),
        "spent_display": timetracking.get("timeSpent"),
        "estimates": {
            "hours": estimate_hours,
            "spent_hours": spent_hours,
            "original_estimate": timetracking.get("originalEstimate"),
            "spent": timetracking.get("timeSpent"),
        },
        "hours_in_status": hours_in_status,
        "returned_after_testing": returned_after_testing,
        "sprint": _pick_sprint(fields),
        "components": _normalize_components(fields),
        "platform": _normalize_platform(fields),
        "labels": fields.get("labels") or [],
        "comments": _normalize_comments(fields),
        "changelog": _normalize_changelog(effective_changelog),
        "links": _normalize_links(fields),
        "risks": risks,
        "url": f"{jira_url}/browse/{issue.get('key')}",
    }
