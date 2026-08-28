"""Build human-readable issue history report from a normalized issue."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.issue import parse_jira_datetime
from reports.mr_analysis import render_mr_markdown

DEFAULT_HISTORY_DIR = Path(__file__).resolve().parents[2] / "reports" / "history"

# Changelog noise — keep timeline focused on workflow-relevant events.
NOISE_FIELDS = {
    "Rank",
    "RemoteIssueLink",
    "WorklogId",
    "WorklogTimeSpent",
    "Attachment",
    "Workflow",
    "timespent",
    "timeestimate",
    "description",
    "summary",
    "Component",
    "Parent",
    "Epic Link",
    "Start date",
    "End date",
    "labels",
    "priority",
    "issuetype",
    "resolution",
    "Performing team",
}

ESTIMATE_FIELDS = {
    "timeoriginalestimate",
    "Original Estimate",
    "Dev (h)",
    "Test (h)",
    "Analytics (h)",
    "QA (h)",
}

SPRINT_FIELDS = {"Sprint"}
FIX_VERSION_FIELDS = {"Fix Version", "fixVersions", "Fix Version/s"}
FLAG_FIELDS = {"Flagged"}

TESTING_STATUSES = {"Testing", "To Test"}
RETURN_TARGET_STATUSES = {
    "Development",
    "Code Review",
    "To Discovery",
    "Discovery",
    "В работе",
    "To Development",
    "New",
}
DEVELOPMENT_STATUSES = {"Development", "В работе", "To Development"}
CLOSED_STATUSES = {"Done", "Canceled"}

MIN_COMMENT_CHARS = 20
COMMENT_PREVIEW_CHARS = 280

# Auto-comments that rarely help a human timeline.
NOISE_COMMENT_AUTHORS = {
    "gitlab-developers",
    "automation for jira",
}
NOISE_COMMENT_PATTERNS = (
    re.compile(r"mentioned this issue in", re.IGNORECASE),
    re.compile(r"^и?сходный запрос", re.IGNORECASE),
)


def build_history_report(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Build history report from already-normalized issue (do not mutate Issue model)."""
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    raw_events = _collect_events(issue)
    timeline = _group_timeline(raw_events)
    status_events = [e for e in raw_events if e["kind"] == "status"]
    returns = [e for e in raw_events if e.get("is_return")]
    cycles = _detect_cycles(status_events)
    summary = _build_summary(issue, status_events, returns)

    return {
        "report_generated_at": generated_at,
        "query_type": "history",
        "query": issue.get("key"),
        "issue": {
            "key": issue.get("key"),
            "title": issue.get("title") or issue.get("summary"),
            "url": issue.get("url"),
            "status": issue.get("status"),
            "type": issue.get("type"),
            "assignee": issue.get("assignee"),
            "author": issue.get("author"),
            "dates": issue.get("dates") or {},
            "estimates": issue.get("estimates") or {},
            "sprint": issue.get("sprint"),
            "labels": issue.get("labels") or [],
            "links": issue.get("links") or [],
        },
        "timeline": timeline,
        "returns": [
            {
                "at": event["at"],
                "author": event.get("author"),
                "from": event.get("from"),
                "to": event.get("to"),
                "text": event.get("text"),
            }
            for event in returns
        ],
        "cycles": cycles,
        "summary": summary,
        "final_state": {
            "status": issue.get("status"),
            "assignee": issue.get("assignee"),
            "estimate_hours": (issue.get("estimates") or {}).get("hours"),
            "spent_hours": (issue.get("estimates") or {}).get("spent_hours"),
            "sprint": (issue.get("sprint") or {}).get("name") if issue.get("sprint") else None,
            "resolved": (issue.get("dates") or {}).get("resolved"),
            "url": issue.get("url"),
        },
    }


def save_history_report(
    report: Dict[str, Any],
    *,
    output_format: str = "both",
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    directory = reports_dir or DEFAULT_HISTORY_DIR
    directory.mkdir(parents=True, exist_ok=True)

    when = datetime.now().astimezone()
    key = _slugify(str(report.get("query") or "issue"))
    stem = f"{when.strftime('%Y-%m-%d_%H-%M')}__{key}__history"
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
        md_path.write_text(render_history_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path

    return paths


def render_history_markdown(report: Dict[str, Any]) -> str:
    issue = report.get("issue") or {}
    key = issue.get("key") or report.get("query") or "issue"
    title = issue.get("title") or ""
    url = issue.get("url") or ""
    summary = report.get("summary") or {}
    final_state = report.get("final_state") or {}

    lines = [
        f"# История {key}",
        "",
        f"**Дата отчёта:** {report.get('report_generated_at', '')}",
        f"**Задача:** [{key}]({url}) — {title}" if url else f"**Задача:** {key} — {title}",
        f"**Тип:** {issue.get('type') or '—'} | **Статус сейчас:** {final_state.get('status') or '—'}",
        f"**Исполнитель:** {final_state.get('assignee') or 'Не назначен'}",
        "",
        "## Хронология",
        "",
    ]

    for group in report.get("timeline") or []:
        at = _format_dt(group.get("at"))
        author = group.get("author") or "—"
        lines.append(f"### {at} — {author}")
        for event in group.get("events") or []:
            prefix = "↩ " if event.get("is_return") else ""
            lines.append(f"- {prefix}{event.get('text')}")
        lines.append("")

    cycles = report.get("cycles") or []
    if cycles:
        lines.extend(["## Циклы", ""])
        for idx, cycle in enumerate(cycles, start=1):
            path = " → ".join(cycle.get("statuses") or [])
            lines.append(
                f"{idx}. **{cycle.get('pattern')}** "
                f"({_format_dt(cycle.get('started_at'))} → {_format_dt(cycle.get('ended_at'))})"
            )
            lines.append(f"   {path}")
        lines.append("")

    returns = report.get("returns") or []
    if returns:
        lines.extend(["## Возвраты после тестирования", ""])
        for event in returns:
            lines.append(
                f"- {_format_dt(event.get('at'))}: "
                f"{event.get('from')} → {event.get('to')} "
                f"({event.get('author') or '—'})"
            )
        lines.append("")

    if report.get("git") is not None or report.get("mr_analysis") is not None:
        lines.append(
            render_mr_markdown(report.get("mr_analysis"), report.get("git")).rstrip()
        )
        lines.append("")

    lines.extend(
        [
            "## Итоговое состояние",
            "",
            f"- Статус: {final_state.get('status') or '—'}",
            f"- Исполнитель: {final_state.get('assignee') or 'Не назначен'}",
            f"- Оценка: {_fmt_hours(final_state.get('estimate_hours'))}",
            f"- Списано: {_fmt_hours(final_state.get('spent_hours'))}",
            f"- Sprint: {final_state.get('sprint') or '—'}",
            f"- Resolved: {_format_dt(final_state.get('resolved')) if final_state.get('resolved') else '—'}",
            "",
            "## Резюме",
            "",
            f"- В работе: {_fmt_days(summary.get('days_in_work'))}",
            f"- Смен исполнителя: {summary.get('assignee_changes', 0)}",
            f"- Возвратов после тестирования: {summary.get('returns_count', 0)}",
            f"- До первого перехода в разработку: {_fmt_days(summary.get('days_to_first_development'))}",
            f"- До закрытия: {_fmt_days(summary.get('days_to_close'))}",
            "",
        ]
    )
    return "\n".join(lines)


def print_history_summary(report: Dict[str, Any]) -> None:
    issue = report.get("issue") or {}
    summary = report.get("summary") or {}
    print(f"История: {issue.get('key')} — {issue.get('title')}")
    print(f"Статус: {issue.get('status')} | Исполнитель: {issue.get('assignee') or 'Не назначен'}")
    print(f"Событий в хронологии: {sum(len(g.get('events') or []) for g in report.get('timeline') or [])}")
    print(f"Возвратов: {summary.get('returns_count', 0)} | Циклов: {len(report.get('cycles') or [])}")
    print(
        f"В работе: {_fmt_days(summary.get('days_in_work'))} | "
        f"До разработки: {_fmt_days(summary.get('days_to_first_development'))} | "
        f"До закрытия: {_fmt_days(summary.get('days_to_close'))}"
    )
    git_meta = report.get("git")
    if git_meta is not None:
        mrs = report.get("merge_requests") or []
        if git_meta.get("ok") and mrs:
            print(f"MR: {len(mrs)} | provider={git_meta.get('provider')}")
        else:
            print(f"MR: нет ({git_meta.get('reason') or 'не найдено'})")


def _collect_events(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    created = (issue.get("dates") or {}).get("created")
    if created:
        events.append(
            {
                "at": created,
                "author": issue.get("author") or "—",
                "kind": "created",
                "text": f"Задача создана ({issue.get('type') or 'без типа'})",
            }
        )

    for history in issue.get("changelog") or []:
        author = _author_name(history.get("author"))
        at = history.get("created")
        if not at:
            continue
        for item in history.get("items") or []:
            event = _map_changelog_item(item, at=at, author=author)
            if event:
                events.append(event)

    for comment in issue.get("comments") or []:
        body = (comment.get("body") or "").strip()
        if len(body) < MIN_COMMENT_CHARS:
            continue
        author = _author_name(comment.get("author")) or "—"
        if _is_noise_comment(author, body):
            continue
        at = comment.get("created")
        if not at:
            continue
        preview = body if len(body) <= COMMENT_PREVIEW_CHARS else body[: COMMENT_PREVIEW_CHARS - 1] + "…"
        preview = re.sub(r"\s+", " ", preview)
        events.append(
            {
                "at": at,
                "author": author,
                "kind": "comment",
                "text": f"Комментарий: {preview}",
            }
        )

    events.sort(key=lambda e: e.get("at") or "")
    return events


def _map_changelog_item(
    item: Dict[str, Any],
    *,
    at: str,
    author: str,
) -> Optional[Dict[str, Any]]:
    field = item.get("field") or ""
    if field in NOISE_FIELDS:
        return None

    from_value = _display_value(field, item.get("from"))
    to_value = _display_value(field, item.get("to"))

    if field == "status":
        is_return = (
            (item.get("from") or "") in TESTING_STATUSES
            and (item.get("to") or "") in RETURN_TARGET_STATUSES
        )
        text = f"Статус: {from_value or '—'} → {to_value or '—'}"
        if is_return:
            text = f"Возврат после тестирования: {from_value} → {to_value}"
        return {
            "at": at,
            "author": author,
            "kind": "status",
            "from": item.get("from"),
            "to": item.get("to"),
            "is_return": is_return,
            "text": text,
        }

    if field == "assignee":
        return {
            "at": at,
            "author": author,
            "kind": "assignee",
            "from": item.get("from"),
            "to": item.get("to"),
            "text": f"Исполнитель: {from_value or 'не назначен'} → {to_value or 'не назначен'}",
        }

    if field in ESTIMATE_FIELDS:
        label = {
            "timeoriginalestimate": "Оценка (original)",
            "Original Estimate": "Оценка (original)",
            "Dev (h)": "Оценка Dev (h)",
            "Test (h)": "Оценка Test (h)",
            "Analytics (h)": "Оценка Analytics (h)",
            "QA (h)": "Оценка QA (h)",
        }.get(field, f"Оценка ({field})")
        return {
            "at": at,
            "author": author,
            "kind": "estimate",
            "field": field,
            "from": item.get("from"),
            "to": item.get("to"),
            "text": f"{label}: {from_value or '—'} → {to_value or '—'}",
        }

    if field in SPRINT_FIELDS:
        return {
            "at": at,
            "author": author,
            "kind": "sprint",
            "from": item.get("from"),
            "to": item.get("to"),
            "text": f"Sprint: {from_value or '—'} → {to_value or '—'}",
        }

    if field in FIX_VERSION_FIELDS:
        return {
            "at": at,
            "author": author,
            "kind": "fix_version",
            "from": item.get("from"),
            "to": item.get("to"),
            "text": f"FixVersion: {from_value or '—'} → {to_value or '—'}",
        }

    if field in FLAG_FIELDS:
        added = bool(to_value) and not from_value
        removed = bool(from_value) and not to_value
        if added:
            text = f"Блокировка добавлена ({to_value})"
        elif removed:
            text = f"Блокировка снята (было: {from_value})"
        else:
            text = f"Блокировка: {from_value or '—'} → {to_value or '—'}"
        return {
            "at": at,
            "author": author,
            "kind": "block",
            "from": item.get("from"),
            "to": item.get("to"),
            "text": text,
        }

    if field == "Link":
        blob = f"{item.get('from') or ''} {item.get('to') or ''}".lower()
        if "block" not in blob and "блокир" not in blob:
            return None
        action = "добавлена" if item.get("to") and not item.get("from") else "изменена"
        if item.get("from") and not item.get("to"):
            action = "снята"
        return {
            "at": at,
            "author": author,
            "kind": "block",
            "from": item.get("from"),
            "to": item.get("to"),
            "text": f"Связь-блокировка {action}: {to_value or from_value or '—'}",
        }

    return None


def _group_timeline(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group events by timestamp + author (Jira often emits separate histories seconds apart)."""
    if not events:
        return []

    groups: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for event in events:
        at = event.get("at") or ""
        author = event.get("author") or "—"
        bucket = _time_bucket(at)

        if (
            current is not None
            and current.get("_bucket") == bucket
            and current.get("author") == author
        ):
            current["events"].append(_public_event(event))
            continue

        current = {
            "at": at,
            "author": author,
            "_bucket": bucket,
            "events": [_public_event(event)],
        }
        groups.append(current)

    for group in groups:
        group.pop("_bucket", None)
    return groups


def _detect_cycles(status_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(status_events) < 3:
        return []

    path: List[Tuple[str, str]] = []
    first_from = status_events[0].get("from")
    first_at = status_events[0].get("at") or ""
    if first_from:
        path.append((first_at, str(first_from)))
    for event in status_events:
        to_status = event.get("to")
        if to_status:
            path.append((event.get("at") or "", str(to_status)))

    if len(path) < 4:
        return []

    cycles: List[Dict[str, Any]] = []
    i = 0
    names = [status for _, status in path]

    while i < len(path) - 3:
        a = names[i]
        b = names[i + 1]
        if a == b:
            i += 1
            continue

        if names[i + 2] == a and names[i + 3] == b:
            end = i + 3
            while end + 2 < len(names) and names[end + 1] == a and names[end + 2] == b:
                end += 2
            if end + 1 < len(names) and names[end + 1] == a:
                end += 1

            statuses = names[i : end + 1]
            if len(statuses) >= 4:
                cycles.append(
                    {
                        "pattern": f"{a} ↔ {b}",
                        "statuses": statuses,
                        "started_at": path[i][0],
                        "ended_at": path[end][0],
                        "transitions": end - i,
                    }
                )
                i = end
                continue
        i += 1

    return cycles


def _build_summary(
    issue: Dict[str, Any],
    status_events: List[Dict[str, Any]],
    returns: List[Dict[str, Any]],
) -> Dict[str, Any]:
    dates = issue.get("dates") or {}
    created = _parse_optional(dates.get("created"))
    resolved = _parse_optional(dates.get("resolved"))
    now = datetime.now(created.tzinfo) if created and created.tzinfo else datetime.now().astimezone()

    end_for_work = resolved or now
    days_in_work = _days_between(created, end_for_work)

    first_dev_at = None
    for event in status_events:
        if (event.get("to") or "") in DEVELOPMENT_STATUSES:
            first_dev_at = _parse_optional(event.get("at"))
            break

    days_to_first_development = _days_between(created, first_dev_at)

    closed = (issue.get("status") or "") in CLOSED_STATUSES
    days_to_close = _days_between(created, resolved) if closed else None

    assignee_changes = 0
    for history in issue.get("changelog") or []:
        for item in history.get("items") or []:
            if item.get("field") == "assignee":
                assignee_changes += 1

    return {
        "days_in_work": days_in_work,
        "assignee_changes": assignee_changes,
        "returns_count": len(returns),
        "days_to_first_development": days_to_first_development,
        "days_to_close": days_to_close,
        "first_development_at": first_dev_at.isoformat(timespec="seconds") if first_dev_at else None,
        "closed": closed,
    }


def _display_value(field: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if field in {"timeoriginalestimate", "Original Estimate"}:
        try:
            seconds = float(text)
            hours = round(seconds / 3600.0, 2)
            return f"{hours}h"
        except ValueError:
            return text
    return text


def _author_name(author: Any) -> str:
    if not author:
        return "—"
    if isinstance(author, dict):
        return author.get("display_name") or author.get("name") or "—"
    return str(author)


def _is_noise_comment(author: str, body: str) -> bool:
    if author.strip().lower() in NOISE_COMMENT_AUTHORS:
        return True
    return any(pattern.search(body) for pattern in NOISE_COMMENT_PATTERNS)


def _public_event(event: Dict[str, Any]) -> Dict[str, Any]:
    public = {
        "kind": event.get("kind"),
        "text": event.get("text"),
    }
    for key in ("from", "to", "field", "is_return"):
        if key in event:
            public[key] = event[key]
    return public


def _time_bucket(value: str) -> str:
    """Bucket to minute so near-simultaneous edits by one person stay together."""
    try:
        dt = parse_jira_datetime(value)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return value[:16] if value else ""


def _parse_optional(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_jira_datetime(value)
    except Exception:
        return None


def _days_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if not start or not end:
        return None
    if start.tzinfo and end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    if end.tzinfo and start.tzinfo is None:
        start = start.replace(tzinfo=end.tzinfo)
    return round((end - start).total_seconds() / 86400.0, 1)


def _format_dt(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return parse_jira_datetime(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def _fmt_days(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value} дн."


def _fmt_hours(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value}h"


def _slugify(value: str) -> str:
    cleaned = value.strip().upper()
    cleaned = re.sub(r"[^A-Z0-9_-]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned[:80] or "issue"


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
