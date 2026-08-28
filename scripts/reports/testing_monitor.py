"""Build testing-monitor report: To Test queue, old/new flow, sprint capacity."""

from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from models.issue import normalize_issue, parse_jira_datetime, pick_estimate_hours, to_float
from models.sprint import normalize_sprint, sprint_date_fragment
from reports.issue_history import (
    CLOSED_STATUSES,
    RETURN_TARGET_STATUSES,
    TESTING_STATUSES,
)
from services import issues as issues_service
from services import sprints as sprints_service
from utils.workdays import business_days, remaining_working_days_until

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports" / "testing-monitor"
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "testing_monitor.yaml"
)

ISSUE_FIELDS = (
    "summary,description,status,assignee,reporter,issuetype,priority,"
    "created,updated,resolutiondate,duedate,labels,issuelinks,fixVersions,"
    "timeoriginalestimate,timespent,timetracking,timeestimate,"
    "customfield_10618,customfield_11330,customfield_11331,"
    "customfield_11332,customfield_11327,*navigable"
)


def load_monitor_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    capacity = raw.get("testing_capacity") or {}
    workday = raw.get("workday") or {}
    return {
        "warning_working_days": float(raw.get("warning_working_days", 3)),
        "critical_working_days": float(raw.get("critical_working_days", 5)),
        "old_test_flow_label": str(raw.get("old_test_flow_label") or "old-test-flow"),
        "closed_statuses": set(raw.get("closed_statuses") or list(CLOSED_STATUSES)),
        "testing_not_ready_statuses": set(
            raw.get("testing_not_ready_statuses")
            or {"New", "To Discovery", "Discovery"}
        ),
        "testing_capacity": {
            "hours_per_working_day": float(capacity.get("hours_per_working_day", 6)),
            "default_issue_hours": float(capacity.get("default_issue_hours", 4)),
            "safety_factor": float(capacity.get("safety_factor", 0.8)),
            "qa_count": float(capacity.get("qa_count", 1)),
        },
        "estimate_quality": {
            "missing_estimate_warning_percent": float(
                (raw.get("estimate_quality") or {}).get(
                    "missing_estimate_warning_percent", 20
                )
            ),
        },
        "workday": {
            "start": time(
                int(workday.get("start_hour", 10)),
                int(workday.get("start_minute", 0)),
            ),
            "end": time(
                int(workday.get("end_hour", 18)),
                int(workday.get("end_minute", 0)),
            ),
            "hours_per_workday": float(workday.get("hours_per_workday", 8)),
        },
        "config_path": str(config_path),
    }


def build_testing_monitor(
    client,
    config,
    *,
    monitor_config: Optional[Dict[str, Any]] = None,
    progress_stream=sys.stderr,
) -> Dict[str, Any]:
    """Fetch To Test queue, classify flows, evaluate sprint capacity."""
    log = progress_stream
    cfg = monitor_config or load_monitor_config()

    print("Подключаюсь к Jira...", file=log)
    server_info = client.get_server_info()
    print(
        f"Jira {server_info.get('version', '?')}, проект {config.project}",
        file=log,
    )

    print("Ищу доску и спринты...", file=log)
    board_id = sprints_service.find_board_id(
        client, config.project, config.board_id or None
    )
    active_short = sprints_service.get_active_sprint(client, board_id)
    active_raw = sprints_service.get_sprint_details(client, int(active_short["id"]))
    active_sprint = normalize_sprint(active_raw)

    next_short = sprints_service.get_nearest_future_sprint(client, board_id)
    next_sprint = None
    if next_short:
        next_raw = sprints_service.get_sprint_details(client, int(next_short["id"]))
        next_sprint = normalize_sprint(next_raw)

    print(f"Активный спринт: {active_sprint.get('name')}", file=log)
    if next_sprint:
        print(f"Следующий спринт: {next_sprint.get('name')}", file=log)
    else:
        print("Следующий спринт: не найден", file=log)

    jql = (
        f'project = {config.project} AND status = "To Test" '
        f'AND issuetype not in (Testing) ORDER BY priority DESC, updated ASC'
    )
    print(f"Ищу задачи в To Test...\nJQL: {jql}", file=log)
    raw_dev = issues_service.search_issues_by_jql(
        client,
        jql,
        fields=ISSUE_FIELDS,
        expand="changelog",
    )
    # Extra safety: keep only development-like types if JQL filter is loose.
    raw_dev = [
        issue
        for issue in raw_dev
        if ((issue.get("fields") or {}).get("issuetype") or {}).get("name")
        not in {"Testing", "Epic", "Sub-task"}
    ]
    print(f"Разработческих задач в To Test: {len(raw_dev)}", file=log)

    print("Загружаю testing-очередь активного спринта...", file=log)
    sprint_raw_issues = issues_service.get_sprint_issues(
        client,
        int(active_sprint["id"]),
        fields=ISSUE_FIELDS,
    )
    project_prefix = f"{config.project}-"
    sprint_testing_queue = _build_sprint_testing_queue(
        client,
        sprint_raw_issues,
        project_prefix=project_prefix,
        closed_statuses=cfg["closed_statuses"],
        jira_url=client.base_url,
        cfg=cfg,
    )

    now = datetime.now().astimezone()
    remaining_days = remaining_working_days_until(
        active_sprint.get("endDate"),
        now=now,
        hours_per_workday=cfg["workday"]["hours_per_workday"],
    )
    capacity_block = _evaluate_capacity(
        remaining_working_days=remaining_days,
        queue_items=sprint_testing_queue,
        cfg=cfg,
    )
    estimate_quality = capacity_block.get("estimate_quality") or {}

    items: List[Dict[str, Any]] = []
    linked_cache: Dict[str, Dict[str, Any]] = {}

    for idx, raw in enumerate(raw_dev, start=1):
        if idx == 1 or idx % 5 == 0 or idx == len(raw_dev):
            print(f"  анализирую {idx}/{len(raw_dev)}…", file=log)
        item = _analyze_dev_issue(
            client,
            raw,
            cfg=cfg,
            now=now,
            active_sprint=active_sprint,
            next_sprint=next_sprint,
            capacity=capacity_block,
            linked_cache=linked_cache,
        )
        items.append(item)

    # Re-evaluate sprint recommendations after all items known (shared capacity budget).
    proposals = _assign_sprint_proposals(
        items,
        capacity=capacity_block,
        active_sprint=active_sprint,
        next_sprint=next_sprint,
        cfg=cfg,
    )

    summary = _build_summary(items, capacity_block, proposals, cfg)
    generated_at = now.isoformat(timespec="seconds")

    return {
        "report_generated_at": generated_at,
        "jira_version": server_info.get("version"),
        "project": config.project,
        "board_id": board_id,
        "active_sprint": active_sprint,
        "next_sprint": next_sprint,
        "config": {
            "warning_working_days": cfg["warning_working_days"],
            "critical_working_days": cfg["critical_working_days"],
            "old_test_flow_label": cfg["old_test_flow_label"],
            "testing_capacity": cfg["testing_capacity"],
            "estimate_quality": cfg["estimate_quality"],
            "config_path": cfg["config_path"],
        },
        "capacity": capacity_block,
        "estimate_quality": estimate_quality,
        "attention_required": estimate_quality.get("attention_required") or [],
        "sprint_testing_queue": sprint_testing_queue,
        "proposed_changes": proposals,
        "summary": summary,
        "items": items,
        "notes": [
            "Рекомендации по спринту — оценка доступной ёмкости, не гарантия "
            "что задача будет протестирована.",
            "Задержку в To Test нельзя автоматически связывать с работой тестировщика.",
            "Праздничный календарь пока не учитывается.",
            "hours_per_working_day лучше откалибровать по истории закрытых testing-задач.",
            "При задачах без оценки расчёт загрузки продолжается с default_issue_hours, "
            "но прогноз не считается точным.",
        ],
    }


def _analyze_dev_issue(
    client,
    raw: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    now: datetime,
    active_sprint: Dict[str, Any],
    next_sprint: Optional[Dict[str, Any]],
    capacity: Dict[str, Any],
    linked_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    issue = normalize_issue(raw, client.base_url)
    labels = list(issue.get("labels") or [])
    old_label = cfg["old_test_flow_label"]
    flow = "old" if old_label in labels else "new"

    to_test = _to_test_timing(issue, now=now, cfg=cfg)
    returns = _return_events(issue)
    fix_versions = _extract_fix_versions(raw.get("fields") or {})
    risks: List[str] = []

    if to_test["working_days_current"] is not None:
        if to_test["working_days_current"] >= cfg["critical_working_days"]:
            risks.append("long_in_to_test")
        elif to_test["working_days_current"] >= cfg["warning_working_days"]:
            risks.append("long_in_to_test")

    if not issue.get("assignee"):
        risks.append("unassigned")

    if returns:
        risks.append("returned_after_testing")

    if fix_versions and _is_near_release(fix_versions, active_sprint):
        risks.append("release_risk")

    result: Dict[str, Any] = {
        "key": issue.get("key"),
        "url": issue.get("url"),
        "summary": issue.get("summary"),
        "type": issue.get("type"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": issue.get("assignee"),
        "labels": labels,
        "flow": flow,
        "sprint": issue.get("sprint"),
        "fix_versions": fix_versions,
        "estimate_hours": issue.get("estimate_hours"),
        "updated": (issue.get("dates") or {}).get("updated"),
        "to_test": to_test,
        "returns_after_testing": returns,
        "returns_count": len(returns),
        "risks": risks,
        "testing_tasks": [],
        "current_testing_tasks": [],
        "previous_open_testing_tasks": [],
        "ambiguous_testing_tasks": [],
        "sprint_recommendations": [],
    }

    if flow == "old":
        # Dev issue itself is the testing queue item.
        result["testing_queue_item"] = {
            "kind": "dev_self",
            "key": issue.get("key"),
            "in_sprint": _sprint_is_active_or_future(issue.get("sprint")),
            "sprint": issue.get("sprint"),
        }
        if not _sprint_is_active_or_future(issue.get("sprint")):
            risks.append("testing_task_not_in_sprint")
            result["sprint_recommendations"].append(
                {
                    "issue_key": issue.get("key"),
                    "reason": "old_flow_dev_not_in_sprint",
                    "fits_current": None,  # filled later
                }
            )
        return result

    # --- new flow ---
    linked_testing_refs = [
        link
        for link in (issue.get("links") or [])
        if (link.get("issue_type") == "Testing")
        or str(link.get("title") or "").lower().startswith("testing")
    ]

    testing_details: List[Dict[str, Any]] = []
    for ref in linked_testing_refs:
        key = ref.get("key")
        if not key:
            continue
        detail = linked_cache.get(key)
        if detail is None:
            raw_linked = issues_service.get_issue_details(
                client, key, fields=ISSUE_FIELDS, expand="changelog"
            )
            detail = _normalize_testing_issue(raw_linked, client.base_url, cfg)
            linked_cache[key] = detail
        testing_details.append(detail)

    closed = cfg["closed_statuses"]
    open_testing = [t for t in testing_details if t.get("status") not in closed]
    closed_testing = [t for t in testing_details if t.get("status") in closed]

    selection = _select_current_testing_tasks(
        open_testing,
        to_test_entries=to_test["entry_times"],
        current_entered_at=to_test["current_entered_at"],
    )

    result["testing_tasks"] = testing_details
    result["testing_tasks_open"] = open_testing
    result["testing_tasks_closed"] = [
        {"key": t.get("key"), "status": t.get("status"), "summary": t.get("summary")}
        for t in closed_testing
    ]
    result["current_testing_tasks"] = selection["current"]
    result["previous_open_testing_tasks"] = selection["previous_open"]
    result["ambiguous_testing_tasks"] = selection["ambiguous"]
    result["selection_notes"] = selection["notes"]

    if not open_testing:
        risks.append("testing_task_missing")
    elif selection["ambiguous"]:
        risks.append("current_testing_task_ambiguous")
    elif not selection["current"] and selection["previous_open"]:
        # Есть открытые задачи прошлых кругов, но нет явной для текущего To Test.
        risks.append("testing_task_missing")

    for task in selection["current"] + (
        selection["ambiguous"] if not selection["current"] else []
    ):
        if task.get("missing_estimate"):
            if "missing_estimate" not in risks:
                risks.append("missing_estimate")
        if task.get("status") in cfg["testing_not_ready_statuses"]:
            if "testing_task_not_ready" not in risks:
                risks.append("testing_task_not_ready")
        if not task.get("assignee"):
            if "unassigned" not in risks:
                risks.append("unassigned")
        if not _sprint_is_active_or_future(task.get("sprint")):
            if "testing_task_not_in_sprint" not in risks:
                risks.append("testing_task_not_in_sprint")
            result["sprint_recommendations"].append(
                {
                    "issue_key": task.get("key"),
                    "parent_key": issue.get("key"),
                    "estimate_hours": task.get("estimate_hours"),
                    "estimate_source": task.get("estimate_source"),
                    "reason": "testing_task_not_in_sprint",
                    "fits_current": None,
                }
            )

    result["risks"] = risks
    return result


def _normalize_testing_issue(
    raw: Dict[str, Any],
    jira_url: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    issue = normalize_issue(raw, jira_url)
    fields = raw.get("fields") or {}
    audit = _audit_issue_estimates(fields, issue.get("summary") or "", issue.get("status") or "")
    default_hours = cfg["testing_capacity"]["default_issue_hours"]

    estimate_hours = audit["estimate_hours"]
    estimate_source = "jira"
    assumption_note = None
    if audit["has_hour_estimate"]:
        estimate_hours = audit["estimate_hours"]
        estimate_source = "jira"
    else:
        estimate_hours = default_hours
        estimate_source = "default"
        if not audit["has_estimate"]:
            assumption_note = (
                f"Для расчёта использовано значение по умолчанию: "
                f"{default_hours} часа."
            )
        else:
            assumption_note = (
                f"Оценка в часах не заполнена (есть: "
                f"{', '.join(audit['estimate_fields_found']) or 'другое поле'}). "
                f"Для расчёта загрузки использовано значение по умолчанию: "
                f"{default_hours} часа."
            )

    remaining = audit["remaining"]
    if remaining.get("hours") is None or not audit["has_hour_estimate"]:
        remaining = {
            "hours": estimate_hours,
            "source": "default" if estimate_source == "default" else "estimate_as_remaining",
            "assumption": True,
        }

    result = {
        "key": issue.get("key"),
        "url": issue.get("url"),
        "summary": issue.get("summary"),
        "type": issue.get("type"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": issue.get("assignee"),
        "sprint": issue.get("sprint"),
        "labels": issue.get("labels") or [],
        "created": (issue.get("dates") or {}).get("created"),
        "updated": (issue.get("dates") or {}).get("updated"),
        "estimate_hours": estimate_hours,
        "estimate_source": estimate_source,
        "has_estimate": audit["has_estimate"],
        "has_hour_estimate": audit["has_hour_estimate"],
        "has_remaining_estimate": audit["has_remaining_estimate"],
        "estimate_fields_found": audit["estimate_fields_found"],
        "spent_hours": issue.get("spent_hours"),
        "remaining_hours": remaining.get("hours"),
        "remaining_source": remaining.get("source"),
        "remaining_assumption": bool(remaining.get("assumption")),
        "missing_estimate": not audit["has_estimate"],
        "assumption_note": assumption_note,
        "fix_versions": _extract_fix_versions(fields),
        "link_directions": [],
    }
    return result


def _audit_issue_estimates(
    fields: Dict[str, Any],
    summary: str,
    status: str,
) -> Dict[str, Any]:
    """Detect whether estimate / remaining are filled for a testing-queue issue."""
    estimate_hours = pick_estimate_hours(fields, summary, status)
    fields_found: List[str] = []

    for field_id, label in (
        ("customfield_11332", "QA (h)"),
        ("customfield_11327", "QA alt (h)"),
        ("customfield_10618", "common estimate (h)"),
        ("customfield_11331", "Dev (h)"),
        ("timeoriginalestimate", "Original Estimate"),
    ):
        value = fields.get(field_id)
        if field_id == "timeoriginalestimate":
            if value is not None:
                fields_found.append(label)
        elif to_float(value) is not None:
            fields_found.append(label)

    tracking = fields.get("timetracking") or {}
    if tracking.get("originalEstimate") or tracking.get("originalEstimateSeconds"):
        if "Original Estimate" not in fields_found:
            fields_found.append("Original Estimate")

    story_points = None
    for sp_field in ("customfield_10016", "customfield_10106", "storyPoints"):
        story_points = to_float(fields.get(sp_field))
        if story_points is not None:
            fields_found.append("Story Points")
            break

    has_hour_estimate = estimate_hours is not None
    has_estimate = has_hour_estimate or story_points is not None

    spent = None
    if fields.get("timespent") is not None:
        spent = round(float(fields["timespent"]) / 3600.0, 2)
    remaining = _remaining_hours(fields, estimate_hours, spent)
    has_remaining = remaining.get("source") == "timetracking.remaining"

    return {
        "estimate_hours": estimate_hours,
        "has_estimate": has_estimate,
        "has_hour_estimate": has_hour_estimate,
        "has_remaining_estimate": has_remaining,
        "estimate_fields_found": fields_found,
        "story_points": story_points,
        "remaining": remaining,
    }


def _remaining_hours(
    fields: Dict[str, Any],
    estimate_hours: Optional[float],
    spent_hours: Optional[float],
) -> Dict[str, Any]:
    tracking = fields.get("timetracking") or {}
    rem_sec = tracking.get("remainingEstimateSeconds")
    if rem_sec is None and fields.get("timeestimate") is not None:
        rem_sec = fields.get("timeestimate")
    if rem_sec is not None:
        return {
            "hours": round(float(rem_sec) / 3600.0, 2),
            "source": "timetracking.remaining",
            "assumption": False,
        }
    if estimate_hours is not None and spent_hours is not None:
        return {
            "hours": round(max(estimate_hours - spent_hours, 0.0), 2),
            "source": "estimate_minus_spent",
            "assumption": True,
        }
    if estimate_hours is not None:
        return {
            "hours": estimate_hours,
            "source": "estimate_as_remaining",
            "assumption": True,
        }
    return {"hours": None, "source": None, "assumption": True}


def _to_test_timing(
    issue: Dict[str, Any],
    *,
    now: datetime,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    events = _status_events(issue)
    segments = _status_segments(issue, events, now=now)
    to_test_segments = [(s, e) for s, e, st in segments if st == "To Test"]
    entry_times = [s for s, _ in to_test_segments]

    current_entered_at = None
    working_days_current = None
    if issue.get("status") == "To Test" and to_test_segments:
        current_entered_at = to_test_segments[-1][0].isoformat(timespec="seconds")
        start = to_test_segments[-1][0]
        working_days_current = round(
            business_days(
                start,
                now,
                hours_per_workday=cfg["workday"]["hours_per_workday"],
                day_start=cfg["workday"]["start"],
                day_end=cfg["workday"]["end"],
            ),
            2,
        )

    past_visits = max(len(to_test_segments) - (1 if issue.get("status") == "To Test" else 0), 0)
    # If currently in To Test, past = all completed previous segments.
    if issue.get("status") == "To Test":
        past_visits = max(len(to_test_segments) - 1, 0)
    else:
        past_visits = len(to_test_segments)

    last_activity = (issue.get("dates") or {}).get("updated")

    return {
        "current_entered_at": current_entered_at,
        "working_days_current": working_days_current,
        "past_visits": past_visits,
        "entry_times": [t.isoformat(timespec="seconds") for t in entry_times],
        "segments": [
            {
                "start": s.isoformat(timespec="seconds"),
                "end": e.isoformat(timespec="seconds"),
            }
            for s, e in to_test_segments
        ],
        "last_activity": last_activity,
    }


def _status_events(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for history in issue.get("changelog") or []:
        at = history.get("created")
        for item in history.get("items") or []:
            if item.get("field") != "status":
                continue
            frm = item.get("from")
            to = item.get("to")
            is_return = (frm or "") in TESTING_STATUSES and (to or "") in RETURN_TARGET_STATUSES
            events.append(
                {
                    "at": at,
                    "at_dt": _parse_dt(at),
                    "from": frm,
                    "to": to,
                    "is_return": is_return,
                }
            )
    events.sort(key=lambda e: e.get("at") or "")
    return events


def _status_segments(
    issue: Dict[str, Any],
    status_events: List[Dict[str, Any]],
    *,
    now: datetime,
) -> List[Tuple[datetime, datetime, str]]:
    created = _parse_dt((issue.get("dates") or {}).get("created")) or now
    end = _parse_dt((issue.get("dates") or {}).get("resolved")) or now
    segments: List[Tuple[datetime, datetime, str]] = []

    if not status_events:
        status = issue.get("status") or "Unknown"
        segments.append((created, end, status))
        return segments

    first = status_events[0]
    current_status = first.get("from") or "Unknown"
    cursor = created
    for event in status_events:
        at = event.get("at_dt")
        if not at:
            continue
        if at > cursor:
            segments.append((cursor, at, str(current_status)))
        current_status = event.get("to") or current_status
        cursor = at
    if end > cursor:
        segments.append((cursor, end, str(current_status)))
    return segments


def _return_events(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = _status_events(issue)
    return [
        {
            "at": e.get("at"),
            "from": e.get("from"),
            "to": e.get("to"),
        }
        for e in events
        if e.get("is_return")
    ]


def _select_current_testing_tasks(
    open_testing: List[Dict[str, Any]],
    *,
    to_test_entries: Sequence[str],
    current_entered_at: Optional[str],
) -> Dict[str, Any]:
    """Pick testing tasks for the current To Test round.

    Rules:
    - Closed tasks already excluded by caller.
    - Multiple open tasks are normal (parallel platforms / sequential rounds).
    - Map each task to a round by created date vs parent To Test entry times.
    - Ambiguous only when mapping to the current transition is unclear.
    """
    notes: List[str] = []
    if not open_testing:
        return {
            "current": [],
            "previous_open": [],
            "ambiguous": [],
            "notes": ["Нет открытых связанных Testing-задач."],
        }

    entries = [_parse_dt(t) for t in to_test_entries if _parse_dt(t)]
    entries = [e for e in entries if e is not None]
    current_start = _parse_dt(current_entered_at) if current_entered_at else (
        entries[-1] if entries else None
    )

    if len(open_testing) == 1:
        only = open_testing[0]
        created = _parse_dt(only.get("created"))
        if created and entries:
            round_idx = _round_index_for_created(created, entries)
            current_round = len(entries) - 1
            if round_idx == current_round or (
                current_start and created >= current_start - timedelta(days=2)
            ):
                notes.append("Единственная открытая Testing-задача — считаем актуальной.")
                return {
                    "current": [{**only, "round_index": round_idx, "round_role": "current"}],
                    "previous_open": [],
                    "ambiguous": [],
                    "notes": notes,
                }
            notes.append(
                "Единственная открытая Testing относится к прошлому кругу; "
                "для текущего To Test актуальной задачи нет."
            )
            return {
                "current": [],
                "previous_open": [
                    {**only, "round_index": round_idx, "round_role": "previous_open"}
                ],
                "ambiguous": [],
                "notes": notes,
            }
        notes.append("Единственная открытая Testing-задача — считаем актуальной.")
        return {
            "current": list(open_testing),
            "previous_open": [],
            "ambiguous": [],
            "notes": notes,
        }

    if not entries:
        notes.append(
            "Не удалось восстановить входы в To Test из changelog — "
            "несколько открытых Testing помечены как неоднозначные."
        )
        return {
            "current": [],
            "previous_open": [],
            "ambiguous": list(open_testing),
            "notes": notes,
        }

    current_round = len(entries) - 1
    current: List[Dict[str, Any]] = []
    previous_open: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []

    for task in open_testing:
        created = _parse_dt(task.get("created"))
        if created is None:
            ambiguous.append(task)
            continue
        round_idx = _round_index_for_created(created, entries)
        enriched = {
            **task,
            "round_index": round_idx,
            "round_role": None,
        }
        if round_idx is None:
            enriched["round_role"] = "ambiguous"
            ambiguous.append(enriched)
        elif round_idx == current_round:
            enriched["round_role"] = "current"
            current.append(enriched)
        elif round_idx < current_round:
            enriched["round_role"] = "previous_open"
            previous_open.append(enriched)
        else:
            # created after a future entry — should not happen
            enriched["round_role"] = "ambiguous"
            ambiguous.append(enriched)

    # Tasks created shortly before current entry (prepared in advance):
    # if none mapped to current_round, promote recent previous_open near current entry.
    if not current and previous_open and current_start:
        promoted = []
        kept_prev = []
        window_start = current_start - timedelta(days=2)
        for task in previous_open:
            created = _parse_dt(task.get("created"))
            if created and created >= window_start:
                promoted.append(
                    {
                        **task,
                        "round_role": "current",
                        "round_index": current_round,
                    }
                )
            else:
                kept_prev.append(task)
        if promoted:
            current = promoted
            previous_open = kept_prev
            notes.append(
                "Часть Testing-задач отнесена к текущему кругу по дате создания "
                "около входа в To Test."
            )

    if not current and ambiguous and not previous_open:
        notes.append(
            "Несколько открытых Testing без явной привязки к текущему To Test."
        )
    elif not current and previous_open:
        notes.append(
            "Открытые Testing относятся к прошлым кругам; для текущего перехода "
            "актуальной задачи не видно."
        )
    elif current:
        notes.append(
            f"Актуальный круг: {len(current)} Testing-задач "
            f"(прошлых открытых: {len(previous_open)}, неоднозначных: {len(ambiguous)})."
        )
        # Multiple current is OK (parallel) — not ambiguous by itself.
        if ambiguous:
            notes.append(
                "Есть задачи без уверенной привязки к кругу — смотри ambiguous_testing_tasks."
            )

    return {
        "current": current,
        "previous_open": previous_open,
        "ambiguous": ambiguous,
        "notes": notes,
    }


def _round_index_for_created(
    created: datetime,
    entries: Sequence[datetime],
) -> Optional[int]:
    """Latest To Test entry_time that is <= created; if before all — round 0."""
    if not entries:
        return None
    idx = None
    for i, entry in enumerate(entries):
        if created >= entry:
            idx = i
        else:
            break
    if idx is None:
        # Created before first To Test — treat as prepared for first round.
        return 0
    return idx


def _build_sprint_testing_queue(
    client,
    sprint_raw_issues: List[Dict[str, Any]],
    *,
    project_prefix: str,
    closed_statuses: Set[str],
    jira_url: str,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Active testing workload already in the sprint."""
    default_hours = cfg["testing_capacity"]["default_issue_hours"]
    queue: List[Dict[str, Any]] = []
    for raw in sprint_raw_issues:
        key = raw.get("key") or ""
        if not key.startswith(project_prefix):
            continue
        fields = raw.get("fields") or {}
        status = (fields.get("status") or {}).get("name")
        if status in closed_statuses:
            continue
        issue_type = (fields.get("issuetype") or {}).get("name")
        labels = fields.get("labels") or []
        is_testing_type = issue_type == "Testing"
        is_old_flow_queue = (
            status == "To Test"
            and cfg["old_test_flow_label"] in labels
            and not is_testing_type
        )
        if not is_testing_type and not is_old_flow_queue:
            continue

        issue = normalize_issue(raw, jira_url, changelog={"histories": []})
        audit = _audit_issue_estimates(
            fields, issue.get("summary") or "", issue.get("status") or ""
        )

        assumption_note = None
        estimate_source = "jira"
        if audit["has_hour_estimate"]:
            estimate_hours = audit["estimate_hours"]
        else:
            estimate_hours = default_hours
            estimate_source = "default"
            if not audit["has_estimate"]:
                assumption_note = (
                    f"Для расчёта использовано значение по умолчанию: "
                    f"{default_hours} часа."
                )
            else:
                assumption_note = (
                    f"Оценка в часах не заполнена. Для расчёта использовано "
                    f"значение по умолчанию: {default_hours} часа."
                )

        remaining = audit["remaining"]
        if remaining.get("hours") is None or not audit["has_hour_estimate"]:
            remaining = {
                "hours": estimate_hours,
                "source": "default" if estimate_source == "default" else "estimate_as_remaining",
                "assumption": True,
            }

        risks: List[str] = []
        if not audit["has_estimate"]:
            risks.append("missing_estimate")

        queue.append(
            {
                "key": issue.get("key"),
                "url": issue.get("url"),
                "summary": issue.get("summary"),
                "type": issue.get("type"),
                "status": issue.get("status"),
                "priority": issue.get("priority"),
                "assignee": issue.get("assignee"),
                "estimate_hours": estimate_hours,
                "estimate_source": estimate_source,
                "has_estimate": audit["has_estimate"],
                "has_hour_estimate": audit["has_hour_estimate"],
                "has_remaining_estimate": audit["has_remaining_estimate"],
                "estimate_fields_found": audit["estimate_fields_found"],
                "remaining_hours": remaining.get("hours"),
                "remaining_source": remaining.get("source"),
                "remaining_assumption": bool(remaining.get("assumption")),
                "missing_estimate": not audit["has_estimate"],
                "assumption_note": assumption_note,
                "risks": risks,
                "kind": "testing_type" if is_testing_type else "old_flow_to_test",
                "fix_versions": _extract_fix_versions(fields),
            }
        )
    return queue


def _evaluate_capacity(
    *,
    remaining_working_days: Optional[float],
    queue_items: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    cap = cfg["testing_capacity"]
    quality_cfg = cfg.get("estimate_quality") or {}
    default_hours = cap["default_issue_hours"]
    warning_percent = float(
        quality_cfg.get("missing_estimate_warning_percent", 20)
    )

    load_hours = 0.0
    missing_issues: List[Dict[str, Any]] = []
    attention_required: List[Dict[str, Any]] = []
    default_used_count = 0
    with_real_hour_estimate = 0
    with_remaining = 0

    for item in queue_items:
        if item.get("missing_estimate") or not item.get("has_estimate"):
            if "missing_estimate" not in (item.get("risks") or []):
                item.setdefault("risks", []).append("missing_estimate")
            item["missing_estimate"] = True
            note = item.get("assumption_note") or (
                f"Для расчёта использовано значение по умолчанию: "
                f"{default_hours} часа."
            )
            attention = {
                "key": item.get("key"),
                "url": item.get("url"),
                "summary": item.get("summary"),
                "status": item.get("status"),
                "kind": item.get("kind"),
                "risk": "missing_estimate",
                "message": "Нет оценки.",
                "assumption_note": note,
                "recommendation": (
                    "Рекомендуется оценить задачу перед планированием тестирования."
                ),
                "default_hours_used": default_hours,
            }
            missing_issues.append(attention)
            attention_required.append(attention)

        hours = item.get("remaining_hours")
        if hours is None:
            hours = item.get("estimate_hours")
        if hours is None:
            hours = default_hours
            item["remaining_hours"] = hours
            item["remaining_assumption"] = True
            item["remaining_source"] = "default"
            item["estimate_source"] = item.get("estimate_source") or "default"

        if item.get("estimate_source") == "default" or not item.get("has_hour_estimate"):
            default_used_count += 1
        if item.get("has_hour_estimate"):
            with_real_hour_estimate += 1
        if item.get("has_remaining_estimate"):
            with_remaining += 1

        load_hours += float(hours)

    total = len(queue_items)
    missing_count = len(missing_issues)
    missing_percent = round((missing_count / total) * 100.0, 1) if total else 0.0

    if total == 0:
        data_quality = "medium"
        confidence = "medium"
        forecast_exact = False
    elif missing_count == 0 and default_used_count == 0:
        data_quality = "high"
        confidence = "high"
        forecast_exact = True
    elif missing_percent >= warning_percent:
        data_quality = "low"
        confidence = "low"
        forecast_exact = False
    elif missing_count > 0 or default_used_count > 0:
        data_quality = "medium"
        confidence = "medium"
        forecast_exact = False
    else:
        data_quality = "high"
        confidence = "high"
        forecast_exact = True

    # Any default/assumption → never present forecast as exact.
    if default_used_count > 0 or missing_count > 0:
        forecast_exact = False
        if confidence == "high":
            confidence = "medium"
            data_quality = "medium"

    warning = None
    if total and missing_percent >= warning_percent:
        warning = (
            f"Более {int(warning_percent) if warning_percent == int(warning_percent) else warning_percent}% "
            f"очереди тестирования не имеет оценки. Расчёт загрузки может быть неточным."
        )

    days = remaining_working_days if remaining_working_days is not None else 0.0
    available = (
        days
        * cap["hours_per_working_day"]
        * cap["safety_factor"]
        * cap["qa_count"]
    )
    free = available - load_hours

    notes = []
    if missing_count:
        notes.append(
            f"{missing_count} задач очереди без оценки — в расчёте default="
            f"{default_hours} ч (явно отмечено в «Требуют внимания»)."
        )
    if default_used_count and default_used_count != missing_count:
        notes.append(
            f"Для {default_used_count} задач использованы допущения по часам "
            f"(нет hour-estimate или remaining)."
        )
    if not forecast_exact:
        notes.append(
            "Прогноз загрузки не считается точным из-за допущений по оценкам."
        )
    notes.append(
        "Расчёт — рекомендация по доступной ёмкости, не обещание срока тестирования."
    )

    estimate_quality = {
        "queue_issue_count": total,
        "missing_estimate_count": missing_count,
        "missing_estimate_percent": missing_percent,
        "missing_estimate_warning_percent": warning_percent,
        "warning": warning,
        "warning_triggered": bool(warning),
        "issues_with_hour_estimate": with_real_hour_estimate,
        "issues_with_remaining_estimate": with_remaining,
        "default_hours_used_count": default_used_count,
        "default_issue_hours": default_hours,
        "data_quality": data_quality,
        "confidence": confidence,
        "forecast_exact": forecast_exact,
        "missing_estimate_issues": missing_issues,
        "attention_required": attention_required,
    }

    return {
        "remaining_working_days": remaining_working_days,
        "hours_per_working_day": cap["hours_per_working_day"],
        "safety_factor": cap["safety_factor"],
        "qa_count": cap["qa_count"],
        "default_issue_hours": default_hours,
        "available_hours": round(available, 2),
        "queue_issue_count": total,
        "queue_hours": round(load_hours, 2),
        "free_hours": round(free, 2),
        "assumptions_count": default_used_count,
        "issues_with_real_estimate": with_real_hour_estimate,
        "missing_estimate_count": missing_count,
        "missing_estimate_percent": missing_percent,
        "data_quality": data_quality,
        "confidence": confidence,
        "forecast_exact": forecast_exact,
        "estimate_quality": estimate_quality,
        "notes": notes,
    }


def _assign_sprint_proposals(
    items: List[Dict[str, Any]],
    *,
    capacity: Dict[str, Any],
    active_sprint: Dict[str, Any],
    next_sprint: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Greedy fill of free capacity for tasks recommended into current sprint."""
    free = float(capacity.get("free_hours") or 0.0)
    default_hours = cfg["testing_capacity"]["default_issue_hours"]
    into_current: List[Dict[str, Any]] = []
    into_next: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    assumptions = 0

    # Flatten recommendations; prefer higher parent priority / longer wait.
    candidates: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
    for item in items:
        wait = float((item.get("to_test") or {}).get("working_days_current") or 0.0)
        for rec in item.get("sprint_recommendations") or []:
            candidates.append((-wait, item, rec))
    candidates.sort(key=lambda x: x[0])

    for _, item, rec in candidates:
        key = rec.get("issue_key")
        hours = rec.get("estimate_hours")
        source = rec.get("estimate_source") or "jira"
        if hours is None:
            hours = default_hours
            source = "default"
            assumptions += 1
        elif source == "default":
            assumptions += 1

        entry = {
            "issue_key": key,
            "parent_key": rec.get("parent_key") or item.get("key"),
            "summary": item.get("summary"),
            "estimate_hours": hours,
            "estimate_source": source,
            "estimate_is_default": source == "default",
            "flow": item.get("flow"),
            "working_days_in_to_test": (item.get("to_test") or {}).get(
                "working_days_current"
            ),
        }

        default_suffix = (
            f" (допущение: default {default_hours} ч)"
            if source == "default"
            else ""
        )

        if free >= float(hours):
            entry["target_sprint"] = {
                "id": active_sprint.get("id"),
                "name": active_sprint.get("name"),
                "state": "active",
            }
            entry["fits_current"] = True
            label = "задача" if item.get("flow") == "old" else "Testing-задача"
            entry["message"] = (
                f"{label} {key} не находится в активном спринте. "
                f"По текущей очереди и оставшемуся времени она помещается "
                f"(оценка {hours} ч{default_suffix}). Добавить её в активный спринт?"
            )
            into_current.append(entry)
            free -= float(hours)
            rec["fits_current"] = True
            rec["target_sprint"] = entry["target_sprint"]
            rec["message"] = entry["message"]
        elif next_sprint:
            entry["target_sprint"] = {
                "id": next_sprint.get("id"),
                "name": next_sprint.get("name"),
                "state": "future",
            }
            entry["fits_current"] = False
            label = "задача" if item.get("flow") == "old" else "Testing-задача"
            entry["message"] = (
                f"До конца текущего спринта осталось "
                f"{capacity.get('remaining_working_days')} раб. дн. "
                f"В очереди {capacity.get('queue_issue_count')} testing-задач на "
                f"{capacity.get('queue_hours')} ч, доступная ёмкость — около "
                f"{capacity.get('available_hours')} ч. {label} {key} оценена в {hours} ч"
                f"{default_suffix} и не помещается.\n\n"
                f"Предлагаю добавить её в следующий спринт "
                f"`{next_sprint.get('name')}`. Применить?"
            )
            into_next.append(entry)
            rec["fits_current"] = False
            rec["target_sprint"] = entry["target_sprint"]
            rec["message"] = entry["message"]
            if "insufficient_capacity" not in item["risks"]:
                item["risks"].append("insufficient_capacity")
        else:
            entry["target_sprint"] = None
            entry["fits_current"] = False
            entry["message"] = (
                f"{key} не помещается в текущий спринт, а следующий спринт "
                f"не создан или не определён. Jira не меняю."
            )
            blocked.append(entry)
            rec["fits_current"] = False
            rec["target_sprint"] = None
            rec["message"] = entry["message"]
            if "insufficient_capacity" not in item["risks"]:
                item["risks"].append("insufficient_capacity")

    if capacity.get("data_quality") in {"low", "medium"}:
        for item in items:
            if item.get("sprint_recommendations") and "insufficient_estimate_data" not in item["risks"]:
                item["risks"].append("insufficient_estimate_data")

    basis = {
        "remaining_working_days": capacity.get("remaining_working_days"),
        "available_hours": capacity.get("available_hours"),
        "queue_hours": capacity.get("queue_hours"),
        "queue_issue_count": capacity.get("queue_issue_count"),
        "default_estimates_used": assumptions,
        "data_quality": capacity.get("data_quality"),
        "confidence": capacity.get("confidence"),
        "forecast_exact": capacity.get("forecast_exact"),
    }

    return {
        "into_current_sprint": into_current,
        "into_next_sprint": into_next,
        "blocked_no_next_sprint": blocked,
        "basis": basis,
        "preview_markdown": _render_proposals_preview(
            into_current, into_next, blocked, basis, active_sprint, next_sprint
        ),
    }


def _render_proposals_preview(
    into_current: List[Dict[str, Any]],
    into_next: List[Dict[str, Any]],
    blocked: List[Dict[str, Any]],
    basis: Dict[str, Any],
    active_sprint: Dict[str, Any],
    next_sprint: Optional[Dict[str, Any]],
) -> str:
    lines = ["### Предлагаемые изменения", ""]
    if into_current:
        lines.append(f"В активный спринт (`{active_sprint.get('name')}`):")
        for e in into_current:
            default_mark = (
                f", допущение default {e['estimate_hours']} ч"
                if e.get("estimate_is_default")
                else ""
            )
            lines.append(
                f"- {e['issue_key']} — помещается, оценка {e['estimate_hours']} ч"
                f"{default_mark}"
            )
        lines.append("")
    if into_next:
        name = (next_sprint or {}).get("name") or "?"
        lines.append(f"В следующий спринт (`{name}`):")
        for e in into_next:
            default_mark = (
                f" (оценка default {e['estimate_hours']} ч)"
                if e.get("estimate_is_default")
                else ""
            )
            lines.append(
                f"- {e['issue_key']} — не помещается в текущий{default_mark}"
            )
        lines.append("")
    if blocked:
        lines.append("Без изменения (следующий спринт не определён):")
        for e in blocked:
            lines.append(f"- {e['issue_key']}")
        lines.append("")
    if not into_current and not into_next and not blocked:
        lines.append("Нет задач для добавления в спринт.")
        lines.append("")

    lines.append("Основание:")
    lines.append(
        f"- до конца текущего спринта: {basis.get('remaining_working_days')} рабочих дней;"
    )
    lines.append(f"- доступная ёмкость: {basis.get('available_hours')} ч;")
    lines.append(
        f"- текущая очередь: {basis.get('queue_hours')} ч "
        f"({basis.get('queue_issue_count')} задач);"
    )
    lines.append(
        f"- использованы оценки по умолчанию для {basis.get('default_estimates_used')} задач;"
    )
    lines.append(
        f"- качество данных: {basis.get('data_quality')}, "
        f"уверенность: {basis.get('confidence')}"
        f"{'' if basis.get('forecast_exact') else ' (прогноз неточный)'}."
    )
    return "\n".join(lines)


def _build_summary(
    items: List[Dict[str, Any]],
    capacity: Dict[str, Any],
    proposals: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    old_flow = sum(1 for i in items if i.get("flow") == "old")
    new_flow = sum(1 for i in items if i.get("flow") == "new")
    long_items = [
        i
        for i in items
        if (i.get("to_test") or {}).get("working_days_current") is not None
        and (i["to_test"]["working_days_current"] >= cfg["warning_working_days"])
    ]
    not_in_sprint = [
        i for i in items if "testing_task_not_in_sprint" in (i.get("risks") or [])
    ]
    risk_counts: Dict[str, int] = {}
    for item in items:
        for risk in item.get("risks") or []:
            risk_counts[risk] = risk_counts.get(risk, 0) + 1

    eq = capacity.get("estimate_quality") or {}
    return {
        "dev_in_to_test": len(items),
        "old_flow": old_flow,
        "new_flow": new_flow,
        "over_warning_threshold": len(long_items),
        "warning_working_days": cfg["warning_working_days"],
        "critical_working_days": cfg["critical_working_days"],
        "testing_tasks_not_in_sprint": len(not_in_sprint),
        "recommended_into_current": len(proposals.get("into_current_sprint") or []),
        "recommended_into_next": len(proposals.get("into_next_sprint") or []),
        "blocked_no_next_sprint": len(proposals.get("blocked_no_next_sprint") or []),
        "queue_issue_count": capacity.get("queue_issue_count"),
        "queue_hours": capacity.get("queue_hours"),
        "available_hours": capacity.get("available_hours"),
        "free_hours": capacity.get("free_hours"),
        "remaining_working_days": capacity.get("remaining_working_days"),
        "data_quality": capacity.get("data_quality"),
        "confidence": capacity.get("confidence"),
        "forecast_exact": capacity.get("forecast_exact"),
        "missing_estimate_count": eq.get("missing_estimate_count")
        or capacity.get("missing_estimate_count"),
        "missing_estimate_percent": eq.get("missing_estimate_percent")
        or capacity.get("missing_estimate_percent"),
        "estimate_quality_warning": eq.get("warning"),
        "risk_counts": risk_counts,
    }


def _sprint_is_active_or_future(sprint: Optional[Dict[str, Any]]) -> bool:
    if not sprint:
        return False
    state = str(sprint.get("state") or "").lower()
    return state in {"active", "future"}


def _extract_fix_versions(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    versions = fields.get("fixVersions") or []
    result = []
    for ver in versions:
        if isinstance(ver, dict):
            result.append(
                {
                    "name": ver.get("name"),
                    "release_date": ver.get("releaseDate"),
                    "released": ver.get("released"),
                }
            )
        elif ver:
            result.append({"name": str(ver)})
    return result


def _is_near_release(
    fix_versions: List[Dict[str, Any]],
    active_sprint: Dict[str, Any],
) -> bool:
    """True if any fixVersion releaseDate falls inside / before active sprint end."""
    end = _parse_dt(active_sprint.get("endDate"))
    if not end:
        return bool(fix_versions)
    for ver in fix_versions:
        rd = _parse_dt(ver.get("release_date"))
        if rd and rd.date() <= end.date():
            return True
    return False


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return parse_jira_datetime(str(value))
    except (TypeError, ValueError):
        return None


def report_basename(report: Dict[str, Any], generated_at: Optional[datetime] = None) -> str:
    when = generated_at or datetime.now().astimezone()
    ts = when.strftime("%Y-%m-%d_%H-%M")
    project = report.get("project") or "PROJECT"
    sprint = report.get("active_sprint") or {}
    start = sprint_date_fragment(sprint.get("startDate"))
    end = sprint_date_fragment(sprint.get("endDate"))
    return f"{ts}__{project}__testing-monitor__sprint-{sprint.get('id')}__{start}__{end}"


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    capacity = report.get("capacity") or {}
    eq = report.get("estimate_quality") or capacity.get("estimate_quality") or {}
    active = report.get("active_sprint") or {}
    nxt = report.get("next_sprint") or {}
    lines = [
        f"# Testing monitor — {report.get('project')}",
        "",
        f"**Дата отчёта:** {report.get('report_generated_at')}",
        f"**Активный спринт:** {active.get('name')} "
        f"({active.get('startDate', '?')} — {active.get('endDate', '?')})",
        f"**Следующий спринт:** {nxt.get('name') or 'не определён'}",
        "",
        "## Сводка",
        "",
        f"- Разработческих задач в To Test: **{summary.get('dev_in_to_test')}**",
        f"- Старый flow: {summary.get('old_flow')}",
        f"- Новый flow: {summary.get('new_flow')}",
        f"- Дольше порога ({summary.get('warning_working_days')} раб. дн.): "
        f"{summary.get('over_warning_threshold')}",
        f"- Testing вне спринта: {summary.get('testing_tasks_not_in_sprint')}",
        f"- Рекомендовано в текущий спринт: {summary.get('recommended_into_current')}",
        f"- Рекомендовано в следующий: {summary.get('recommended_into_next')}",
        f"- Очередь: {summary.get('queue_issue_count')} задач / "
        f"{summary.get('queue_hours')} ч",
        f"- Доступная ёмкость: {summary.get('available_hours')} ч "
        f"(свободно ~{summary.get('free_hours')} ч)",
        f"- Качество данных: {summary.get('data_quality')} "
        f"(уверенность {summary.get('confidence')}"
        f"{'' if summary.get('forecast_exact') else ', прогноз неточный'})",
        f"- Без оценки в очереди: {summary.get('missing_estimate_count')} "
        f"({summary.get('missing_estimate_percent')}%)",
        "",
        "## Ёмкость",
        "",
        f"- Осталось рабочих дней: {capacity.get('remaining_working_days')}",
        f"- hours_per_working_day: {capacity.get('hours_per_working_day')}",
        f"- safety_factor: {capacity.get('safety_factor')}",
        f"- Допущений по оценке: {capacity.get('assumptions_count')}",
        "",
    ]
    for note in capacity.get("notes") or []:
        lines.append(f"- _{note}_")
    lines.append("")

    # --- Качество прогноза ---
    if eq.get("missing_estimate_count") or eq.get("default_hours_used_count"):
        lines.extend(["## Качество прогноза", ""])
        lines.append(
            f"- Задач без оценки: **{eq.get('missing_estimate_count', 0)}** / "
            f"{eq.get('queue_issue_count', 0)} "
            f"({eq.get('missing_estimate_percent', 0)}%)"
        )
        lines.append(
            f"- С hour-оценкой: {eq.get('issues_with_hour_estimate', 0)}; "
            f"с Remaining Estimate: {eq.get('issues_with_remaining_estimate', 0)}"
        )
        lines.append(
            f"- Использован default ({eq.get('default_issue_hours')} ч): "
            f"{eq.get('default_hours_used_count', 0)} задач"
        )
        lines.append(
            f"- Уровень доверия к прогнозу загрузки: **{eq.get('confidence')}** "
            f"(качество данных: {eq.get('data_quality')})"
        )
        if not eq.get("forecast_exact"):
            lines.append("- Прогноз **не считается точным** из-за допущений.")
        lines.append("")
        if eq.get("warning"):
            lines.append(f"> {eq['warning']}")
            lines.append("")
        missing_list = eq.get("missing_estimate_issues") or []
        if missing_list:
            lines.append("Список задач без оценки:")
            for issue in missing_list:
                lines.append(
                    f"- [{issue.get('key')}]({issue.get('url')}) — "
                    f"{issue.get('summary') or '—'}"
                )
            lines.append("")

    # --- Требуют внимания ---
    attention = report.get("attention_required") or eq.get("attention_required") or []
    if attention:
        lines.extend(["## Требуют внимания", ""])
        for issue in attention:
            lines.append(
                f"### [{issue.get('key')}]({issue.get('url')}) — "
                f"{issue.get('summary') or ''}"
            )
            lines.append("")
            lines.append(f"❗ {issue.get('message') or 'Нет оценки.'}")
            lines.append("")
            if issue.get("assumption_note"):
                lines.append(issue["assumption_note"])
                lines.append("")
            if issue.get("recommendation"):
                lines.append(issue["recommendation"])
                lines.append("")
        lines.append("")

    proposals = report.get("proposed_changes") or {}
    preview = proposals.get("preview_markdown")
    if preview:
        lines.extend([preview, ""])

    lines.extend(["## Задачи", ""])
    for item in report.get("items") or []:
        to_test = item.get("to_test") or {}
        risks = ",".join(item.get("risks") or []) or "—"
        lines.append(
            f"### [{item.get('key')}]({item.get('url')}) — {item.get('summary')}"
        )
        lines.append("")
        lines.append(
            f"- Flow: `{item.get('flow')}` · приоритет: {item.get('priority')} · "
            f"assignee: {item.get('assignee') or '—'}"
        )
        lines.append(
            f"- В To Test: {to_test.get('working_days_current')} раб. дн. "
            f"(прошлых заходов: {to_test.get('past_visits')})"
        )
        lines.append(f"- FixVersion: {_fmt_versions(item.get('fix_versions'))}")
        lines.append(f"- Риски: `{risks}`")
        if item.get("flow") == "new":
            current = item.get("current_testing_tasks") or []
            if current:
                lines.append("- Актуальные Testing:")
                for t in current:
                    sprint_name = (t.get("sprint") or {}).get("name") or "вне спринта"
                    est = t.get("estimate_hours")
                    src = t.get("estimate_source")
                    est_label = f"{est} ч"
                    if src == "default" or t.get("missing_estimate"):
                        est_label += " (допущение: default)"
                    lines.append(
                        f"  - [{t.get('key')}]({t.get('url')}) [{t.get('status')}] "
                        f"· {sprint_name} · оценка {est_label}"
                    )
                    if t.get("assumption_note"):
                        lines.append(f"    - ❗ {t['assumption_note']}")
            prev = item.get("previous_open_testing_tasks") or []
            if prev:
                lines.append("- Открытые Testing прошлых кругов:")
                for t in prev:
                    lines.append(f"  - {t.get('key')} [{t.get('status')}]")
            amb = item.get("ambiguous_testing_tasks") or []
            if amb:
                lines.append("- Неоднозначные Testing:")
                for t in amb:
                    lines.append(f"  - {t.get('key')} [{t.get('status')}]")
            for note in item.get("selection_notes") or []:
                lines.append(f"- _{note}_")
        if item.get("returns_count"):
            lines.append(f"- Возвратов после тестирования: {item.get('returns_count')}")
        lines.append("")

    for note in report.get("notes") or []:
        lines.append(f"> {note}")
    lines.append("")
    return "\n".join(lines)


def _fmt_versions(versions: Optional[List[Dict[str, Any]]]) -> str:
    if not versions:
        return "—"
    return ", ".join(v.get("name") or "?" for v in versions)


def save_testing_monitor_report(
    report: Dict[str, Any],
    *,
    output_format: str = "both",
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    directory = reports_dir or DEFAULT_REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stem = report_basename(report)
    paths: Dict[str, Path] = {}

    if output_format in {"json", "both"}:
        json_path = _unique_path(directory / f"{stem}.json")
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths["json"] = json_path

    if output_format in {"markdown", "both"}:
        if "json" in paths:
            md_path = paths["json"].with_suffix(".md")
        else:
            md_path = _unique_path(directory / f"{stem}.md")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path

    return paths


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def print_testing_monitor_summary(report: Dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    eq = report.get("estimate_quality") or {}
    active = report.get("active_sprint") or {}
    print(f"Проект: {report.get('project')}")
    print(f"Спринт: {active.get('name')}")
    print(f"В To Test: {summary.get('dev_in_to_test')} "
          f"(old={summary.get('old_flow')}, new={summary.get('new_flow')})")
    print(
        f"Дольше порога: {summary.get('over_warning_threshold')}; "
        f"вне спринта: {summary.get('testing_tasks_not_in_sprint')}"
    )
    print(
        f"Ёмкость: очередь {summary.get('queue_hours')} ч / "
        f"доступно {summary.get('available_hours')} ч "
        f"(качество={summary.get('data_quality')}, "
        f"доверие={summary.get('confidence')}"
        f"{'' if summary.get('forecast_exact') else ', прогноз неточный'})"
    )
    print(
        f"Оценки: без оценки {summary.get('missing_estimate_count')} "
        f"({summary.get('missing_estimate_percent')}%); "
        f"default использован для {eq.get('default_hours_used_count', '?')} задач"
    )
    if eq.get("warning"):
        print(f"⚠ {eq['warning']}")
    print(
        f"Рекомендации: в текущий={summary.get('recommended_into_current')}, "
        f"в следующий={summary.get('recommended_into_next')}"
    )
    attention = report.get("attention_required") or []
    if attention:
        print(f"\nТребуют внимания ({len(attention)}):")
        for issue in attention[:10]:
            print(f"  - {issue.get('key')}: нет оценки (default {issue.get('default_hours_used')} ч)")
        if len(attention) > 10:
            print(f"  … и ещё {len(attention) - 10}")
    proposals = report.get("proposed_changes") or {}
    preview = proposals.get("preview_markdown")
    if preview:
        print()
        print(preview)
