"""Build employee work analysis report from Jira issues + assignee history."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from models.issue import STUCK_OVER_ESTIMATE_RATIO, normalize_issue, parse_jira_datetime
from reports.issue_history import (
    CLOSED_STATUSES,
    DEVELOPMENT_STATUSES,
    RETURN_TARGET_STATUSES,
    TESTING_STATUSES,
)
from services import issues as issues_service
from services import metadata as meta
from services.merge_requests import discover_mr_refs_from_jira

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports" / "employees"

CODE_REVIEW_STATUSES = {"Code Review"}
PATTERN_MIN_ISSUES = 2
LARGE_ESTIMATE_HOURS = 16.0
MANY_STATUS_TRANSITIONS = 8
MANY_ASSIGNEE_CHANGES = 3
# Этапы считаем в рабочих днях: пн–пт, окно 10:00–18:00 (8ч = 1 раб. день).
# Праздники пока не исключаем.
WORKDAY_START = time(10, 0)
WORKDAY_END = time(18, 0)
HOURS_PER_WORKDAY = 8.0
LONG_STAGE_WORKDAYS = 2.0
LONG_DEV_WORKDAYS = 4.0
DEVELOPER_ROLES = {
    "developer",
    "backend",
    "frontend",
    "web",
    "ios",
    "android",
    "mobile",
}

DIRECTION_FROM_TYPE = {
    "DevelopmentB": "backend",
    "DevelopmentF": "web",
    "Bug": "bug",
    "Testing": "qa",
    "Analysis": "analytics",
    "Specification": "analytics",
    "Design": "design",
    "Documentation": "docs",
    "Research": "research",
    "Epic": "epic",
}

TITLE_DIRECTION_RE = re.compile(
    r"\[(android|ios|web|backend|back|frontend|front|qa|sa|design|mobileweb|webview|admin)\]",
    re.IGNORECASE,
)

TITLE_DIRECTION_MAP = {
    "android": "android",
    "ios": "ios",
    "web": "web",
    "backend": "backend",
    "back": "backend",
    "frontend": "web",
    "front": "web",
    "qa": "qa",
    "sa": "analytics",
    "design": "design",
    "mobileweb": "web",
    "webview": "web",
    "admin": "admin",
}


def parse_period(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    months: Optional[int] = None,
    month: Optional[str] = None,
    quarter: Optional[str] = None,
    today: Optional[date] = None,
) -> Tuple[datetime, datetime, str]:
    """Resolve analysis window. Returns (start, end_exclusive-ish end-of-day, label)."""
    now = datetime.now().astimezone()
    today_d = today or now.date()

    if month:
        year_s, month_s = month.split("-", 1)
        year_i, month_i = int(year_s), int(month_s)
        start = datetime(year_i, month_i, 1, tzinfo=now.tzinfo)
        if month_i == 12:
            end = datetime(year_i + 1, 1, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        else:
            end = datetime(year_i, month_i + 1, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        return start, end, f"{year_i}-{month_i:02d}"

    if quarter:
        # 2026-Q2 or Q2 / 2026Q2
        q = quarter.strip().upper().replace(" ", "")
        match = re.match(r"^(?:(\d{4})-?)?Q([1-4])$", q)
        if not match:
            raise ValueError(f"Неверный квартал: {quarter} (ожидается 2026-Q2)")
        year_i = int(match.group(1) or today_d.year)
        q_i = int(match.group(2))
        start_month = (q_i - 1) * 3 + 1
        start = datetime(year_i, start_month, 1, tzinfo=now.tzinfo)
        end_month = start_month + 3
        if end_month > 12:
            end = datetime(year_i + 1, end_month - 12, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        else:
            end = datetime(year_i, end_month, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        return start, end, f"{year_i}-Q{q_i}"

    if months is not None:
        if months < 1:
            raise ValueError("--months должен быть >= 1")
        end = datetime(today_d.year, today_d.month, today_d.day, 23, 59, 59, tzinfo=now.tzinfo)
        start_d = today_d - timedelta(days=30 * months)
        start = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=now.tzinfo)
        return start, end, f"last-{months}-months"

    if date_from and date_to:
        start = _parse_day_start(date_from, now.tzinfo)
        end = _parse_day_end(date_to, now.tzinfo)
        if end < start:
            raise ValueError("--to раньше --from")
        return start, end, f"{start.date().isoformat()}__{end.date().isoformat()}"

    if date_from and not date_to:
        start = _parse_day_start(date_from, now.tzinfo)
        end = datetime(today_d.year, today_d.month, today_d.day, 23, 59, 59, tzinfo=now.tzinfo)
        return start, end, f"{start.date().isoformat()}__{end.date().isoformat()}"

    # Default: last 2 months
    end = datetime(today_d.year, today_d.month, today_d.day, 23, 59, 59, tzinfo=now.tzinfo)
    start_d = today_d - timedelta(days=60)
    start = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=now.tzinfo)
    return start, end, "last-2-months"


def build_employee_analysis(
    client: Any,
    *,
    employee_query: str,
    period_start: datetime,
    period_end: datetime,
    period_label: str,
    project_key: str,
    project_config: Dict[str, Any],
    discover_mrs: bool = True,
    with_git: bool = False,
    git_client: Any = None,
    progress_stream=sys.stderr,
) -> Dict[str, Any]:
    """Fetch issues where employee was assignee in period and build report."""
    log = progress_stream
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    resolved = meta.resolve_user(
        client,
        employee_query,
        people_aliases=project_config.get("people_aliases") or {},
    )
    if not resolved.get("ok"):
        candidates = resolved.get("candidates") or []
        hint = ""
        if candidates:
            options = ", ".join(
                f"{c.get('name')} ({c.get('display_name')})" for c in candidates
            )
            hint = f" Кандидаты: {options}"
        raise ValueError(f"{resolved.get('error')}{hint}")

    user = resolved["user"]
    username = user.get("name") or ""
    display_name = user.get("displayName") or username
    match_names = _employee_match_names(user)

    role = _resolve_role(username, project_config)
    print(f"Сотрудник: {display_name} ({username}), роль={role or '—'}", file=log)
    print(
        f"Период: {period_start.date().isoformat()} … {period_end.date().isoformat()} ({period_label})",
        file=log,
    )

    jql = _candidate_issues_jql(
        project_key=project_key,
        username=username,
        period_start=period_start,
        period_end=period_end,
    )
    print(f"JQL (кандидаты): {jql}", file=log)
    print(
        "Примечание: assignee was/changed на этой Jira недоступен (500) — "
        "фильтр по истории assignee делается локально.",
        file=log,
    )

    # One paginated search with changelog (no comments — lighter payload).
    candidate_fields = (
        "summary,status,assignee,reporter,issuetype,priority,"
        "created,updated,resolutiondate,duedate,labels,issuelinks,"
        "timeoriginalestimate,timespent,timetracking,"
        "customfield_10618,customfield_11330,customfield_11331,"
        "customfield_11332,customfield_11327,*navigable"
    )
    raw_candidates = issues_service.search_issues_by_jql(
        client,
        jql,
        fields=candidate_fields,
        expand="changelog",
    )
    print(f"Кандидатов по JQL: {len(raw_candidates)}", file=log)

    raw_issues: List[Dict[str, Any]] = []
    for raw in raw_candidates:
        fields = raw.get("fields") or {}
        assignee_name = (fields.get("assignee") or {}).get("displayName")
        created = _parse_dt(fields.get("created"))
        changelog = raw.get("changelog") or {}
        if _changelog_has_assignee_overlap(
            changelog,
            match_names=match_names,
            period_start=period_start,
            period_end=period_end,
            current_assignee=_clean_name(assignee_name),
            created=created,
        ):
            raw_issues.append(raw)

    print(f"После фильтра assignee-истории: {len(raw_issues)}", file=log)

    analyzed: List[Dict[str, Any]] = []
    skipped_no_overlap = 0
    mr_refs_all: List[Dict[str, Any]] = []
    data_quality = {
        "issues_without_estimate": 0,
        "issues_without_spent": 0,
        "issues_without_changelog": 0,
        "issues_assignee_match_by_display_name_only": 0,
        "spent_time_is_issue_level": True,
        "estimate_is_issue_level": True,
        "candidate_jql_hits": len(raw_candidates),
        "notes": [
            "estimate/spent — на уровне задачи целиком, не только периода назначения сотрудника",
            "метрики этапов — в рабочих днях (пн–пт 10:00–18:00, 8ч = 1 раб. день; праздники не исключаем)",
            "метрики этапов считаются только на пересечении статусов с периодами assignee сотрудника",
            "JQL assignee was/changed на этой Jira возвращает 500 — кандидаты: "
            "текущий assignee / worklogAuthor / status changed BY, затем фильтр changelog",
        ],
    }

    for idx, raw in enumerate(raw_issues, start=1):
        if idx == 1 or idx % 10 == 0 or idx == len(raw_issues):
            print(f"  анализирую {idx}/{len(raw_issues)}…", file=log)

        normalized = normalize_issue(raw, client.base_url)
        if not normalized.get("changelog"):
            data_quality["issues_without_changelog"] += 1

        item = _analyze_issue_for_employee(
            normalized,
            match_names=match_names,
            period_start=period_start,
            period_end=period_end,
        )
        if item is None:
            skipped_no_overlap += 1
            continue

        if item.get("estimate_hours") is None:
            data_quality["issues_without_estimate"] += 1
        if item.get("spent_hours") is None:
            data_quality["issues_without_spent"] += 1

        mr_refs: List[Dict[str, Any]] = []
        if discover_mrs:
            try:
                mr_refs = discover_mr_refs_from_jira(
                    issue_key=normalized["key"],
                    raw_issue=raw,
                    jira_client=client,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                data_quality.setdefault("mr_discovery_errors", []).append(
                    f"{normalized.get('key')}: {exc}"
                )
        item["related_mrs"] = [
            {"ref": r.get("ref"), "url": r.get("url"), "source": r.get("source")}
            for r in mr_refs
        ]
        item["related_mrs_count"] = len(mr_refs)
        for ref in mr_refs:
            mr_refs_all.append({**ref, "issue_key": normalized.get("key")})

        analyzed.append(item)

    unique_mr_refs = _unique_by_ref(mr_refs_all)
    summary = _build_summary(analyzed)
    patterns = _detect_patterns(analyzed)
    review_cases = _build_review_cases(analyzed)

    is_developer = (role or "").lower() in DEVELOPER_ROLES
    if not role:
        # Infer from workload if role not configured
        directions = summary.get("by_direction") or {}
        devish = sum(
            directions.get(k, 0) for k in ("backend", "web", "ios", "android", "bug")
        )
        if analyzed and devish >= max(2, len(analyzed) // 2):
            is_developer = True
            role = role or "developer (inferred)"

    git_offer = None
    git_block: Optional[Dict[str, Any]] = None
    if is_developer and unique_mr_refs:
        git_offer = {
            "show": True,
            "mr_count": len(unique_mr_refs),
            "message": (
                f"Найдено {len(unique_mr_refs)} Merge Request за выбранный период.\n\n"
                "Могу дополнительно проанализировать:\n\n"
                "* количество замечаний на ревью;\n"
                "* категории замечаний;\n"
                "* повторяющиеся замечания;\n"
                "* циклы исправлений;\n"
                "* скорость реакции на ревью;\n"
                "* взаимодействие с ревьюерами.\n\n"
                "Выполнить Git-анализ?"
            ),
        }
    else:
        git_offer = {
            "show": False,
            "mr_count": len(unique_mr_refs),
            "reason": (
                "Сотрудник не разработчик"
                if not is_developer
                else "Связанные Merge Request не найдены"
            ),
        }

    if with_git:
        git_block = _run_git_analysis(
            client=client,
            git_client=git_client,
            raw_issues=raw_issues,
            analyzed_keys={i["key"] for i in analyzed},
            log=log,
        )

    slug = _slugify_employee(username or display_name)
    report = {
        "report_generated_at": generated_at,
        "query_type": "employee_analysis",
        "employee": {
            "query": employee_query,
            "username": username,
            "display_name": display_name,
            "email": user.get("emailAddress"),
            "role": role,
            "is_developer": is_developer,
            "slug": slug,
            "resolve_warning": resolved.get("warning"),
        },
        "period": {
            "label": period_label,
            "start": period_start.isoformat(timespec="seconds"),
            "end": period_end.isoformat(timespec="seconds"),
        },
        "project": project_key,
        "jql": jql,
        "summary": summary,
        "patterns": patterns,
        "review_cases": review_cases,
        "issues": analyzed,
        "git_offer": git_offer,
        "related_mrs_preview": [
            {
                "issue_key": r.get("issue_key"),
                "ref": r.get("ref"),
                "url": r.get("url"),
                "source": r.get("source"),
            }
            for r in unique_mr_refs[:50]
        ],
        "data_quality": {
            **data_quality,
            "jql_hits": data_quality.get("candidate_jql_hits"),
            "analyzed": len(analyzed),
            "skipped_no_assignee_overlap": skipped_no_overlap,
            "after_assignee_filter": len(raw_issues),
        },
    }
    if git_block is not None:
        report["git"] = git_block

    return report


def save_employee_report(
    report: Dict[str, Any],
    *,
    output_format: str = "both",
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    employee = report.get("employee") or {}
    slug = employee.get("slug") or "employee"
    directory = (reports_dir or DEFAULT_REPORTS_DIR) / slug
    directory.mkdir(parents=True, exist_ok=True)

    when = datetime.now().astimezone()
    period = (report.get("period") or {}).get("label") or "period"
    stem = f"{when.strftime('%Y-%m-%d_%H-%M')}__{period}"
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
        md_path.write_text(render_employee_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path

    return paths


def render_employee_markdown(report: Dict[str, Any]) -> str:
    employee = report.get("employee") or {}
    period = report.get("period") or {}
    summary = report.get("summary") or {}
    lines = [
        f"# Анализ работы: {employee.get('display_name') or employee.get('username')}",
        "",
        f"**Дата отчёта:** {report.get('report_generated_at', '')}",
        f"**Сотрудник:** {employee.get('display_name')} (`{employee.get('username')}`)",
        f"**Роль:** {employee.get('role') or '—'}",
        f"**Период:** {period.get('start', '')[:10]} — {period.get('end', '')[:10]} ({period.get('label')})",
        f"**Проект:** {report.get('project')}",
        "",
        "## Сводка",
        "",
        f"- Задач в анализе: {summary.get('issues_total', 0)}",
        f"- Завершённые: {summary.get('completed', 0)} | Незавершённые: {summary.get('incomplete', 0)}",
        f"- Суммарная оценка: {_fmt_hours(summary.get('estimate_hours_total'))}",
        f"- Списано (issue-level): {_fmt_hours(summary.get('spent_hours_total'))}",
        f"- Без оценки: {summary.get('without_estimate', 0)}",
        f"- С превышением оценки: {summary.get('over_estimate', 0)}",
        f"- Возвратов после тестирования (сумма): {summary.get('returns_total', 0)}",
        f"- Carry-over (гипотеза): {summary.get('carry_over_candidates', 0)}",
        "",
        "### По типам",
        "",
    ]
    for name, count in (summary.get("by_type") or {}).items():
        lines.append(f"- {name}: {count}")
    lines.extend(["", "### По направлениям", ""])
    for name, count in (summary.get("by_direction") or {}).items():
        lines.append(f"- {name}: {count}")

    lines.extend(["", "## Факты", ""])
    for fact in summary.get("facts") or []:
        lines.append(f"- {fact}")

    lines.extend(["", "## Повторяющиеся паттерны", ""])
    patterns = report.get("patterns") or []
    if not patterns:
        lines.append("_Подтверждённых паттернов (на ≥2 задачах) не найдено._")
    else:
        for pattern in patterns:
            keys = ", ".join(pattern.get("issue_keys") or [])
            lines.append(f"- **{pattern.get('title')}** — {pattern.get('detail')}")
            lines.append(f"  Задачи: {keys}")

    lines.extend(["", "## Кейсы для ручного разбора", ""])
    cases = report.get("review_cases") or []
    if not cases:
        lines.append("_Отдельных кейсов по порогам не найдено._")
    else:
        for case in cases:
            url = case.get("url") or ""
            key = case.get("key")
            reasons = ", ".join(case.get("reasons") or [])
            title = case.get("title") or ""
            link = f"[{key}]({url})" if url else key
            lines.append(f"- {link} — {title}")
            lines.append(f"  Причины: {reasons}")

    lines.extend(["", "## Крупные / важные задачи", ""])
    for item in summary.get("notable_issues") or []:
        url = item.get("url") or ""
        key = item.get("key")
        link = f"[{key}]({url})" if url else key
        lines.append(
            f"- {link} — {item.get('title')} "
            f"(оценка {_fmt_hours(item.get('estimate_hours'))}, статус {item.get('status')})"
        )

    offer = report.get("git_offer") or {}
    if offer.get("show"):
        lines.extend(["", "## Git-анализ (предложение)", "", offer.get("message") or ""])

    dq = report.get("data_quality") or {}
    lines.extend(
        [
            "",
            "## Качество данных",
            "",
            f"- JQL hits: {dq.get('jql_hits')} | в анализе: {dq.get('analyzed')} | "
            f"без overlap assignee: {dq.get('skipped_no_assignee_overlap')}",
            f"- Без оценки: {dq.get('issues_without_estimate')} | без spent: {dq.get('issues_without_spent')}",
            f"- Без changelog: {dq.get('issues_without_changelog')}",
        ]
    )
    for note in dq.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def print_employee_summary(report: Dict[str, Any]) -> None:
    employee = report.get("employee") or {}
    summary = report.get("summary") or {}
    print(
        f"Анализ: {employee.get('display_name')} ({employee.get('username')}) | "
        f"роль={employee.get('role') or '—'}"
    )
    period = report.get("period") or {}
    print(f"Период: {period.get('start', '')[:10]} — {period.get('end', '')[:10]}")
    print(
        f"Задач: {summary.get('issues_total', 0)} "
        f"(done {summary.get('completed', 0)} / open {summary.get('incomplete', 0)})"
    )
    print(
        f"Оценка: {_fmt_hours(summary.get('estimate_hours_total'))} | "
        f"Spent: {_fmt_hours(summary.get('spent_hours_total'))} | "
        f"Возвраты: {summary.get('returns_total', 0)}"
    )
    print(f"Паттерны: {len(report.get('patterns') or [])} | "
          f"Кейсы: {len(report.get('review_cases') or [])}")
    offer = report.get("git_offer") or {}
    if offer.get("show"):
        print(f"Git-offer: да ({offer.get('mr_count')} MR)")
    else:
        print(f"Git-offer: нет ({offer.get('reason')})")


# --- per-issue analysis -------------------------------------------------


def _analyze_issue_for_employee(
    issue: Dict[str, Any],
    *,
    match_names: Set[str],
    period_start: datetime,
    period_end: datetime,
) -> Optional[Dict[str, Any]]:
    assignee_intervals = _assignee_intervals(issue, match_names)
    overlapping: List[Tuple[datetime, datetime]] = []
    for start, end in assignee_intervals:
        pair = _intersect(start, end, period_start, period_end)
        if pair[0] is not None and pair[1] is not None and pair[1] > pair[0]:
            overlapping.append((pair[0], pair[1]))

    if not overlapping:
        return None

    status_events = _status_events(issue)
    status_segments = _status_segments(issue, status_events)
    stage_hours = _stage_hours_during(status_segments, overlapping)
    returns = _returns_during(status_events, overlapping)
    status_transitions = _count_status_transitions(status_events, overlapping)
    blocks = _blocks_during(issue, overlapping)
    dependencies = _dependency_links(issue)
    sprint_changes = _sprint_changes_during(issue, overlapping)
    assignee_changes_total = _count_assignee_changes(issue)

    first_assigned = overlapping[0][0]
    first_dev = _first_development_while_assigned(status_events, overlapping)
    hours_to_start = None
    if first_dev and first_assigned:
        hours_to_start = round((first_dev - first_assigned).total_seconds() / 3600.0, 2)
        if hours_to_start < 0:
            hours_to_start = 0.0

    joined_mid_flight = _joined_mid_flight(status_events, assignee_intervals)

    estimate = (issue.get("estimates") or {}).get("hours")
    if estimate is None:
        estimate = issue.get("estimate_hours")
    spent = (issue.get("estimates") or {}).get("spent_hours")
    if spent is None:
        spent = issue.get("spent_hours")

    over_estimate = bool(
        estimate and spent and spent > estimate * STUCK_OVER_ESTIMATE_RATIO
    )

    direction = _infer_direction(issue)
    completed = (issue.get("status") or "") in CLOSED_STATUSES or (
        issue.get("status") or ""
    ) == "To Prod"

    return {
        "key": issue.get("key"),
        "title": issue.get("title") or issue.get("summary"),
        "url": issue.get("url"),
        "type": issue.get("type"),
        "status": issue.get("status"),
        "direction": direction,
        "completed": completed,
        "assignee_periods": [
            {"from": a.isoformat(timespec="seconds"), "to": b.isoformat(timespec="seconds")}
            for a, b in overlapping
        ],
        "assignee_hours_in_period": round(
            sum((b - a).total_seconds() for a, b in overlapping) / 3600.0, 2
        ),
        "estimate_hours": estimate,
        "spent_hours": spent,
        "over_estimate": over_estimate,
        "status_transitions": _status_transition_list(status_events, overlapping),
        "status_transitions_count": status_transitions,
        "hours_to_start_work": hours_to_start,
        "workdays_in_development": stage_hours.get("development"),
        "workdays_in_code_review": stage_hours.get("code_review"),
        "workdays_in_testing": stage_hours.get("testing"),
        # alias для обратной совместимости со старыми отчётами / скиллами
        "hours_in_development": stage_hours.get("development"),
        "hours_in_code_review": stage_hours.get("code_review"),
        "hours_in_testing": stage_hours.get("testing"),
        "returns_after_testing": returns,
        "returns_count": len(returns),
        "blocks": blocks,
        "dependencies": dependencies,
        "sprint_changes_while_assigned": sprint_changes,
        "carry_over_candidate": len(sprint_changes) >= 1,
        "assignee_changes_total": assignee_changes_total,
        "joined_mid_flight": joined_mid_flight,
        "final_state": {
            "status": issue.get("status"),
            "assignee": issue.get("assignee"),
            "sprint": (issue.get("sprint") or {}).get("name") if issue.get("sprint") else None,
            "resolved": (issue.get("dates") or {}).get("resolved"),
        },
    }


def _assignee_intervals(
    issue: Dict[str, Any],
    match_names: Set[str],
) -> List[Tuple[datetime, datetime]]:
    created = _parse_dt((issue.get("dates") or {}).get("created"))
    if not created:
        created = datetime.now().astimezone()
    end_default = _parse_dt((issue.get("dates") or {}).get("resolved")) or datetime.now().astimezone()

    changes: List[Tuple[datetime, Optional[str], Optional[str]]] = []
    for history in issue.get("changelog") or []:
        at = _parse_dt(history.get("created"))
        if not at:
            continue
        for item in history.get("items") or []:
            if item.get("field") != "assignee":
                continue
            changes.append((at, _clean_name(item.get("from")), _clean_name(item.get("to"))))
    changes.sort(key=lambda x: x[0])

    intervals: List[Tuple[datetime, datetime]] = []
    if not changes:
        current = _clean_name(issue.get("assignee"))
        if current and _name_matches(current, match_names):
            intervals.append((created, end_default))
        return intervals

    # Before first change the assignee was changes[0].from (may be empty = unassigned)
    current = changes[0][1]
    cursor = created
    for at, _frm, to in changes:
        if current and _name_matches(current, match_names) and at > cursor:
            intervals.append((cursor, at))
        current = to
        cursor = at
    if current and _name_matches(current, match_names) and end_default > cursor:
        intervals.append((cursor, end_default))
    return intervals


def _status_events(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for history in issue.get("changelog") or []:
        at = history.get("created")
        author = None
        author_obj = history.get("author")
        if isinstance(author_obj, dict):
            author = author_obj.get("display_name") or author_obj.get("name")
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
                    "author": author,
                    "is_return": is_return,
                }
            )
    events.sort(key=lambda e: e.get("at") or "")
    return events


def _status_segments(
    issue: Dict[str, Any],
    status_events: List[Dict[str, Any]],
) -> List[Tuple[datetime, datetime, str]]:
    created = _parse_dt((issue.get("dates") or {}).get("created")) or datetime.now().astimezone()
    end = _parse_dt((issue.get("dates") or {}).get("resolved")) or datetime.now().astimezone()
    segments: List[Tuple[datetime, datetime, str]] = []

    if not status_events:
        status = issue.get("status") or "Unknown"
        segments.append((created, end, status))
        return segments

    # Initial status = first from, else unknown
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


def _business_hours(start: datetime, end: datetime) -> float:
    """Сумма часов внутри рабочих окон пн–пт 10:00–18:00."""
    if end <= start:
        return 0.0
    if start.tzinfo is not None and end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    elif end.tzinfo is not None and start.tzinfo is None:
        start = start.replace(tzinfo=end.tzinfo)

    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() < 5:
            day_start = datetime.combine(day, WORKDAY_START, tzinfo=start.tzinfo)
            day_end = datetime.combine(day, WORKDAY_END, tzinfo=start.tzinfo)
            a = max(start, day_start)
            b = min(end, day_end)
            if b > a:
                total += (b - a).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def _business_days(start: datetime, end: datetime) -> float:
    return _business_hours(start, end) / HOURS_PER_WORKDAY


def _stage_hours_during(
    segments: List[Tuple[datetime, datetime, str]],
    assignee_windows: List[Tuple[datetime, datetime]],
) -> Dict[str, float]:
    """Длительности этапов в рабочих днях (не календарных часах)."""
    buckets = {"development": 0.0, "code_review": 0.0, "testing": 0.0}
    for seg_start, seg_end, status in segments:
        for win_start, win_end in assignee_windows:
            a, b = _intersect(seg_start, seg_end, win_start, win_end)
            if a is None or b is None or b <= a:
                continue
            days = _business_days(a, b)
            if status in DEVELOPMENT_STATUSES:
                buckets["development"] += days
            elif status in CODE_REVIEW_STATUSES:
                buckets["code_review"] += days
            elif status in TESTING_STATUSES:
                buckets["testing"] += days
    return {k: round(v, 2) for k, v in buckets.items()}


def _returns_during(
    status_events: List[Dict[str, Any]],
    windows: List[Tuple[datetime, datetime]],
) -> List[Dict[str, Any]]:
    result = []
    for event in status_events:
        if not event.get("is_return"):
            continue
        at = event.get("at_dt")
        if not at or not _in_any_window(at, windows):
            continue
        result.append(
            {
                "at": event.get("at"),
                "from": event.get("from"),
                "to": event.get("to"),
                "author": event.get("author"),
            }
        )
    return result


def _count_status_transitions(
    status_events: List[Dict[str, Any]],
    windows: List[Tuple[datetime, datetime]],
) -> int:
    return sum(
        1
        for event in status_events
        if event.get("at_dt") and _in_any_window(event["at_dt"], windows)
    )


def _status_transition_list(
    status_events: List[Dict[str, Any]],
    windows: List[Tuple[datetime, datetime]],
) -> List[Dict[str, Any]]:
    result = []
    for event in status_events:
        at = event.get("at_dt")
        if not at or not _in_any_window(at, windows):
            continue
        result.append(
            {
                "at": event.get("at"),
                "from": event.get("from"),
                "to": event.get("to"),
                "is_return": bool(event.get("is_return")),
            }
        )
    return result


def _blocks_during(
    issue: Dict[str, Any],
    windows: List[Tuple[datetime, datetime]],
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for history in issue.get("changelog") or []:
        at = _parse_dt(history.get("created"))
        if not at or not _in_any_window(at, windows):
            continue
        for item in history.get("items") or []:
            field = item.get("field") or ""
            if field == "Flagged":
                blocks.append(
                    {
                        "at": history.get("created"),
                        "kind": "flagged",
                        "from": item.get("from"),
                        "to": item.get("to"),
                    }
                )
            elif field == "Link":
                blob = f"{item.get('from') or ''} {item.get('to') or ''}".lower()
                if "block" in blob or "блокир" in blob:
                    blocks.append(
                        {
                            "at": history.get("created"),
                            "kind": "link_block",
                            "from": item.get("from"),
                            "to": item.get("to"),
                        }
                    )
    return blocks


def _dependency_links(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for link in issue.get("links") or []:
        link_type = (link.get("type") or "").lower()
        if "block" in link_type or "завис" in link_type or "depend" in link_type:
            result.append(
                {
                    "key": link.get("key"),
                    "type": link.get("type"),
                    "direction": link.get("direction"),
                    "status": link.get("status"),
                    "title": link.get("title"),
                }
            )
    return result


def _sprint_changes_during(
    issue: Dict[str, Any],
    windows: List[Tuple[datetime, datetime]],
) -> List[Dict[str, Any]]:
    changes = []
    for history in issue.get("changelog") or []:
        at = _parse_dt(history.get("created"))
        if not at or not _in_any_window(at, windows):
            continue
        for item in history.get("items") or []:
            if item.get("field") != "Sprint":
                continue
            frm = item.get("from")
            to = item.get("to")
            if frm == to:
                continue
            changes.append({"at": history.get("created"), "from": frm, "to": to})
    return changes


def _count_assignee_changes(issue: Dict[str, Any]) -> int:
    count = 0
    for history in issue.get("changelog") or []:
        for item in history.get("items") or []:
            if item.get("field") == "assignee":
                count += 1
    return count


def _first_development_while_assigned(
    status_events: List[Dict[str, Any]],
    windows: List[Tuple[datetime, datetime]],
) -> Optional[datetime]:
    for event in status_events:
        if (event.get("to") or "") not in DEVELOPMENT_STATUSES:
            continue
        at = event.get("at_dt")
        if at and _in_any_window(at, windows):
            return at
    return None


def _joined_mid_flight(
    status_events: List[Dict[str, Any]],
    assignee_intervals: List[Tuple[datetime, datetime]],
) -> bool:
    """True if employee became assignee after the task already entered Development."""
    if not assignee_intervals:
        return False
    first_own = assignee_intervals[0][0]
    for event in status_events:
        at = event.get("at_dt")
        if not at or at >= first_own:
            continue
        if (event.get("to") or "") in DEVELOPMENT_STATUSES:
            return True
    return False


# --- aggregation / patterns ---------------------------------------------


def _build_summary(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Counter = Counter()
    by_direction: Counter = Counter()
    estimate_total = 0.0
    spent_total = 0.0
    has_estimate = 0
    has_spent = 0
    without_estimate = 0
    over_estimate = 0
    completed = 0
    incomplete = 0
    returns_total = 0
    long_stages = 0
    carry_over = 0

    for item in issues:
        by_type[item.get("type") or "—"] += 1
        by_direction[item.get("direction") or "other"] += 1
        if item.get("completed"):
            completed += 1
        else:
            incomplete += 1
        est = item.get("estimate_hours")
        spent = item.get("spent_hours")
        if est is None:
            without_estimate += 1
        else:
            estimate_total += est
            has_estimate += 1
        if spent is not None:
            spent_total += spent
            has_spent += 1
        if item.get("over_estimate"):
            over_estimate += 1
        returns_total += int(item.get("returns_count") or 0)
        if item.get("carry_over_candidate"):
            carry_over += 1
        for key in ("workdays_in_development", "workdays_in_code_review", "workdays_in_testing"):
            val = item.get(key)
            if val is not None and val > LONG_STAGE_WORKDAYS:
                long_stages += 1
                break

    notable = sorted(
        issues,
        key=lambda i: (
            -(i.get("estimate_hours") or 0),
            -(i.get("spent_hours") or 0),
            i.get("key") or "",
        ),
    )[:8]
    top_directions = [name for name, _ in by_direction.most_common(3)]

    facts = [
        f"В анализ попало {len(issues)} задач, где сотрудник был исполнителем в периоде",
        f"Завершённые: {completed}, незавершённые: {incomplete}",
        f"Сумма оценок (issue-level): {_fmt_hours(round(estimate_total, 2) if has_estimate else None)}",
        f"Сумма списанного (issue-level): {_fmt_hours(round(spent_total, 2) if has_spent else None)}",
        f"Задач без оценки: {without_estimate}",
        f"Задач с превышением оценки (spent > estimate×{STUCK_OVER_ESTIMATE_RATIO}): {over_estimate}",
        f"Задач с долгими этапами (> {LONG_STAGE_WORKDAYS} раб. дн. в Dev/CR/Testing при assignee): {long_stages}",
        f"Суммарно возвратов после тестирования в периоды assignee: {returns_total}",
        f"Кандидатов в carry-over (смена Sprint при assignee): {carry_over}",
    ]
    if top_directions:
        facts.append(f"Основные направления: {', '.join(top_directions)}")

    return {
        "issues_total": len(issues),
        "completed": completed,
        "incomplete": incomplete,
        "by_type": dict(by_type.most_common()),
        "by_direction": dict(by_direction.most_common()),
        "main_directions": top_directions,
        "estimate_hours_total": round(estimate_total, 2) if has_estimate else None,
        "spent_hours_total": round(spent_total, 2) if has_spent else None,
        "without_estimate": without_estimate,
        "over_estimate": over_estimate,
        "long_stage_issues": long_stages,
        "returns_total": returns_total,
        "carry_over_candidates": carry_over,
        "facts": facts,
        "notable_issues": [
            {
                "key": i.get("key"),
                "title": i.get("title"),
                "url": i.get("url"),
                "status": i.get("status"),
                "estimate_hours": i.get("estimate_hours"),
                "spent_hours": i.get("spent_hours"),
                "direction": i.get("direction"),
            }
            for i in notable
        ],
    }


def _detect_patterns(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    patterns: List[Dict[str, Any]] = []

    def add(pattern_id: str, title: str, detail: str, keys: Sequence[str]) -> None:
        if len(keys) < PATTERN_MIN_ISSUES:
            return
        patterns.append(
            {
                "id": pattern_id,
                "title": title,
                "detail": detail,
                "issue_keys": list(keys),
                "issue_count": len(keys),
            }
        )

    long_cr = [
        i["key"]
        for i in issues
        if (i.get("workdays_in_code_review") or 0) > LONG_STAGE_WORKDAYS
    ]
    add(
        "long_code_review",
        "Задачи регулярно долго находятся в Code Review",
        f"Code Review > {LONG_STAGE_WORKDAYS} раб. дн. в период назначения",
        long_cr,
    )

    returned = [i["key"] for i in issues if (i.get("returns_count") or 0) > 0]
    add(
        "returns_after_testing",
        "Несколько задач возвращались после тестирования",
        "Был переход Testing/To Test → Development/CR/… пока сотрудник был исполнителем",
        returned,
    )

    by_dir: Dict[str, List[str]] = {}
    for item in issues:
        direction = item.get("direction") or "other"
        by_dir.setdefault(direction, []).append(item["key"])
    for direction, keys in by_dir.items():
        if direction in {"other", "bug", "epic"}:
            continue
        if len(keys) >= max(PATTERN_MIN_ISSUES, int(len(issues) * 0.4)):
            add(
                f"direction_{direction}",
                f"Сотрудник часто берёт задачи направления «{direction}»",
                f"{len(keys)} из {len(issues)} задач",
                keys,
            )

    mid = [i["key"] for i in issues if i.get("joined_mid_flight")]
    add(
        "joined_mid_flight",
        "Регулярно подключается к уже начатым задачам",
        "Назначение произошло после входа задачи в Development",
        mid,
    )

    large = [
        i["key"]
        for i in issues
        if (i.get("estimate_hours") or 0) >= LARGE_ESTIMATE_HOURS
        or (i.get("spent_hours") or 0) >= LARGE_ESTIMATE_HOURS
    ]
    add(
        "large_tasks",
        "Работает преимущественно с большими задачами",
        f"Оценка или spent ≥ {LARGE_ESTIMATE_HOURS}h",
        large,
    )

    long_dev = [
        i["key"]
        for i in issues
        if (i.get("workdays_in_development") or 0) > LONG_DEV_WORKDAYS
    ]
    add(
        "long_development",
        "Задачи регулярно долго находятся в разработке",
        f"Development > {LONG_DEV_WORKDAYS} раб. дн. в период назначения",
        long_dev,
    )

    return patterns


def _build_review_cases(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cases = []
    for item in issues:
        reasons = []
        if (item.get("returns_count") or 0) >= 2:
            reasons.append(f"несколько возвратов ({item['returns_count']})")
        if (item.get("workdays_in_code_review") or 0) > LONG_STAGE_WORKDAYS:
            reasons.append(
                f"долгое Code Review ({item['workdays_in_code_review']} раб. дн.)"
            )
        if item.get("blocks"):
            reasons.append(f"блокировки ({len(item['blocks'])})")
        if (item.get("assignee_changes_total") or 0) >= MANY_ASSIGNEE_CHANGES:
            reasons.append(f"частая смена исполнителя ({item['assignee_changes_total']})")
        if (item.get("status_transitions_count") or 0) >= MANY_STATUS_TRANSITIONS:
            reasons.append(f"много переходов статуса ({item['status_transitions_count']})")
        if item.get("over_estimate"):
            reasons.append(
                f"превышение оценки (spent {item.get('spent_hours')}h / "
                f"estimate {item.get('estimate_hours')}h)"
            )
        if not reasons:
            continue
        cases.append(
            {
                "key": item.get("key"),
                "title": item.get("title"),
                "url": item.get("url"),
                "status": item.get("status"),
                "reasons": reasons,
            }
        )
    cases.sort(key=lambda c: (-len(c.get("reasons") or []), c.get("key") or ""))
    return cases


def _run_git_analysis(
    *,
    client: Any,
    git_client: Any,
    raw_issues: List[Dict[str, Any]],
    analyzed_keys: Set[str],
    log,
) -> Dict[str, Any]:
    if git_client is None:
        return {
            "enabled": True,
            "ok": False,
            "reason": "Git-клиент не передан",
            "merge_requests": [],
        }

    from reports.mr_analysis import build_mr_analysis
    from services.merge_requests import collect_merge_requests

    all_mrs: List[Dict[str, Any]] = []
    errors: List[str] = []
    by_issue: Dict[str, Any] = {}

    for raw in raw_issues:
        key = raw.get("key")
        if key not in analyzed_keys:
            continue
        print(f"  git: {key}…", file=log)
        try:
            bundle = collect_merge_requests(
                issue_key=key,
                raw_issue=raw,
                jira_client=client,
                git_client=git_client,
            )
            mrs = bundle.get("merge_requests") or []
            by_issue[key] = {
                "ok": bundle.get("ok"),
                "reason": bundle.get("reason"),
                "mr_count": len(mrs),
            }
            for mr in mrs:
                all_mrs.append({**mr, "issue_key": key})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {exc}")

    analysis = build_mr_analysis(
        {
            "ok": bool(all_mrs),
            "provider": getattr(git_client, "provider", None),
            "reason": None if all_mrs else "MR не найдены при полном git-анализе",
            "merge_requests": all_mrs,
        }
    )

    return {
        "enabled": True,
        "ok": bool(all_mrs),
        "provider": getattr(git_client, "provider", None),
        "reason": None if all_mrs else "MR не найдены при полном git-анализе",
        "merge_requests": analysis.get("merge_requests") or [],
        "mr_analysis": analysis,
        "by_issue": by_issue,
        "errors": errors or None,
        "summary": {
            "mr_count": len(all_mrs),
            "issues_with_mr": sum(1 for v in by_issue.values() if v.get("mr_count")),
        },
    }


# --- helpers ------------------------------------------------------------


def _candidate_issues_jql(
    *,
    project_key: str,
    username: str,
    period_start: datetime,
    period_end: datetime,
) -> str:
    """Build candidate JQL when assignee history operators are broken.

    This Jira instance returns HTTP 500 for `assignee was` / `assignee changed`.
    Do not over-fetch all issues updated in the window (that JQL times out on CAT2).
    Candidates: current assignee, worklog author, or status changes by the user;
    changelog still filters to assignee overlap in the period.
    """
    # Pad so late changelog / status moves still pull the issue in.
    start = (period_start - timedelta(days=14)).strftime("%Y-%m-%d")
    return (
        f"project = {project_key} AND ("
        f"assignee = {username} OR "
        f"worklogAuthor = {username} OR "
        f'status changed BY {username} AFTER "{start}"'
        f") ORDER BY updated DESC"
    )


def _changelog_has_assignee_overlap(
    changelog: Dict[str, Any],
    *,
    match_names: Set[str],
    period_start: datetime,
    period_end: datetime,
    current_assignee: Optional[str],
    created: Optional[datetime],
) -> bool:
    """Quick check on raw Jira changelog whether employee was assignee in period."""
    changes: List[Tuple[datetime, Optional[str], Optional[str]]] = []
    for history in changelog.get("histories") or []:
        at = _parse_dt(history.get("created"))
        if not at:
            continue
        for item in history.get("items") or []:
            if item.get("field") != "assignee":
                continue
            changes.append(
                (
                    at,
                    _clean_name(item.get("fromString")),
                    _clean_name(item.get("toString")),
                )
            )
    changes.sort(key=lambda x: x[0])

    start_cursor = created or period_start
    end_default = period_end
    if not changes:
        if not (current_assignee and _name_matches(current_assignee, match_names)):
            return False
        a, b = _intersect(created or period_start, end_default, period_start, period_end)
        return bool(a is not None and b is not None and b > a)

    current = changes[0][1]
    cursor = start_cursor
    for at, _frm, to in changes:
        if current and _name_matches(current, match_names):
            a, b = _intersect(cursor, at, period_start, period_end)
            if a is not None and b is not None and b > a:
                return True
        current = to
        cursor = at
    if current and _name_matches(current, match_names):
        a, b = _intersect(cursor, end_default, period_start, period_end)
        if a is not None and b is not None and b > a:
            return True
    return False


def _resolve_role(username: str, project_config: Dict[str, Any]) -> Optional[str]:
    roles = project_config.get("people_roles") or {}
    if username in roles:
        return roles[username]
    # try case-insensitive
    for key, value in roles.items():
        if key.lower() == username.lower():
            return value
    return None


def _employee_match_names(user: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    for key in ("displayName", "name", "emailAddress"):
        value = _clean_name(user.get(key))
        if value:
            names.add(value.lower())
    display = _clean_name(user.get("displayName"))
    if display:
        # Also match "Фамилия" alone if unique enough — keep full tokens
        parts = [p for p in re.split(r"\s+", display) if len(p) > 2]
        if parts:
            names.add(parts[0].lower())  # usually surname in RU order
    return names


def _name_matches(candidate: Optional[str], match_names: Set[str]) -> bool:
    if not candidate:
        return False
    low = candidate.lower().strip()
    if low in match_names:
        return True
    # changelog sometimes shortens names
    for name in match_names:
        if name and (name in low or low in name):
            # avoid tiny false positives
            if len(name) >= 4 and len(low) >= 4:
                return True
    return False


def _infer_direction(issue: Dict[str, Any]) -> str:
    title = issue.get("title") or issue.get("summary") or ""
    match = TITLE_DIRECTION_RE.search(title)
    if match:
        return TITLE_DIRECTION_MAP.get(match.group(1).lower(), match.group(1).lower())
    issue_type = issue.get("type") or ""
    if issue_type in DIRECTION_FROM_TYPE:
        return DIRECTION_FROM_TYPE[issue_type]
    for label in issue.get("labels") or []:
        low = str(label).lower()
        if low in TITLE_DIRECTION_MAP:
            return TITLE_DIRECTION_MAP[low]
    return "other"


def _unique_by_ref(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    result = []
    for ref in refs:
        key = ref.get("ref") or ref.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def _intersect(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return None, None
    return start, end


def _in_any_window(point: datetime, windows: List[Tuple[datetime, datetime]]) -> bool:
    return any(start <= point <= end for start, end in windows)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_jira_datetime(value)
    except Exception:
        return None


def _clean_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_day_start(value: str, tzinfo) -> datetime:
    d = date.fromisoformat(value.strip())
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tzinfo)


def _parse_day_end(value: str, tzinfo) -> datetime:
    d = date.fromisoformat(value.strip())
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tzinfo)


def _fmt_hours(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value}h"


def _slugify_employee(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned[:60] or "employee"


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
