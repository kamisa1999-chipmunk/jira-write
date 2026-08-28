"""Sprint capacity: closed CAT2 sprints → tasks / hours / worklogs by person."""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jira_client import JiraClient
from models.issue import (
    is_testing_task,
    normalize_issue,
    parse_jira_datetime,
    seconds_to_hours,
)
from models.sprint import normalize_sprint, sprint_date_fragment
from services import issues as issues_service
from services import sprints as sprints_service
from utils.workdays import HOURS_PER_WORKDAY, business_days

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

TEAM: Dict[str, Dict[str, str]] = {
    "kafanova_m": {"short": "Маша", "direction": "SA"},
    "guminichenko_d": {"short": "Дима", "direction": "Web"},
    "gabisov_r": {"short": "Руслан", "direction": "Web"},
    "pushkarev_a": {"short": "Андрей", "direction": "Web"},
    "lebedev_m": {"short": "Максим", "direction": "iOS"},
    "gromazina_o": {"short": "Оля", "direction": "Android"},
    "goldebaev_a": {"short": "Саша", "direction": "Backend"},
    "filetkina_a": {"short": "Настя", "direction": "QA"},
    "cherkasov_ko": {"short": "Костя", "direction": "QA"},
}

DONE_STATUSES = {"Done", "To Prod"}
DEV_REACHED_STATUSES = {
    "Done",
    "To Prod",
    "To Test",
    "Testing",
    "Code Review",
    "Delivery",
    "To Launch",
    "To Design Review",
    "Design Review",
}

ISSUE_FIELDS = (
    "summary,status,assignee,issuetype,priority,"
    "timeoriginalestimate,timespent,timetracking,"
    "customfield_10618,customfield_11330,customfield_11331,"
    "customfield_11332,customfield_11327"
)


def _median(values: Sequence[float]) -> Optional[float]:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return round(float(statistics.median(cleaned)), 1)


def _mean(values: Sequence[float]) -> Optional[float]:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return round(float(statistics.mean(cleaned)), 1)


def _username(issue: Dict[str, Any]) -> Optional[str]:
    details = issue.get("assignee_details") or {}
    return details.get("name")


def _is_qa_work(issue: Dict[str, Any]) -> bool:
    return bool(issue.get("is_testing_task")) or (issue.get("type") == "Testing")


def _counts_for_person(username: str, issue: Dict[str, Any]) -> bool:
    if _username(issue) != username:
        return False
    role_dir = TEAM[username]["direction"]
    qa_work = _is_qa_work(issue)
    if role_dir == "QA":
        return qa_work
    return not qa_work


def _sprint_window(sprint: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
    start = sprint.get("startDate")
    end = sprint.get("endDate")
    start_dt = parse_jira_datetime(start) if start else None
    end_dt = parse_jira_datetime(end) if end else None
    return start_dt, end_dt


def _workdays(sprint: Dict[str, Any]) -> float:
    start_dt, end_dt = _sprint_window(sprint)
    if not start_dt or not end_dt:
        return 0.0
    return round(business_days(start_dt, end_dt), 1)


def _list_board_sprints(client: JiraClient, board_id: int, state: str) -> List[Dict[str, Any]]:
    return list(
        client.paginate(
            f"/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": state},
            max_results=50,
        )
    )


def _is_cat2_dated(name: str) -> bool:
    return "CAT2" in (name or "") and "-" in (name or "")


def select_sprints(
    client: JiraClient,
    board_id: int,
    *,
    closed_count: int = 4,
) -> List[Dict[str, Any]]:
    closed = [
        normalize_sprint(item)
        for item in _list_board_sprints(client, board_id, "closed")
        if _is_cat2_dated(item.get("name") or "")
    ]
    closed.sort(key=lambda s: s.get("startDate") or "")
    picked = closed[-closed_count:] if closed_count else closed

    active_raw = _list_board_sprints(client, board_id, "active")
    active = [
        normalize_sprint(item)
        for item in active_raw
        if _is_cat2_dated(item.get("name") or "")
    ]
    seen = {s["id"] for s in picked}
    for sprint in active:
        if sprint["id"] not in seen:
            picked.append(sprint)
    return picked


def _empty_person_bucket(username: str) -> Dict[str, Any]:
    meta = TEAM[username]
    return {
        "username": username,
        "short": meta["short"],
        "direction": meta["direction"],
        "assigned": 0,
        "done": 0,
        "dev_reached": 0,
        "estimate_assigned": 0.0,
        "estimate_done": 0.0,
        "estimate_dev_reached": 0.0,
        "spent_assigned": 0.0,
        "worklog_hours": 0.0,
        "worklog_issues": 0,
        "keys_assigned": [],
        "keys_done": [],
        "keys_dev_reached": [],
    }


def _add_issue_to_bucket(bucket: Dict[str, Any], issue: Dict[str, Any]) -> None:
    estimate = issue.get("estimate_hours") or 0.0
    spent = issue.get("spent_hours") or 0.0
    status = issue.get("status") or ""
    key = issue.get("key")
    bucket["assigned"] += 1
    bucket["estimate_assigned"] += estimate
    bucket["spent_assigned"] += spent
    bucket["keys_assigned"].append(key)
    if status in DONE_STATUSES:
        bucket["done"] += 1
        bucket["estimate_done"] += estimate
        bucket["keys_done"].append(key)
    if status in DEV_REACHED_STATUSES:
        bucket["dev_reached"] += 1
        bucket["estimate_dev_reached"] += estimate
        bucket["keys_dev_reached"].append(key)


def _round_bucket(bucket: Dict[str, Any]) -> None:
    for field in (
        "estimate_assigned",
        "estimate_done",
        "estimate_dev_reached",
        "spent_assigned",
        "worklog_hours",
    ):
        bucket[field] = round(float(bucket[field]), 1)


def fetch_worklogs_for_period(
    client: JiraClient,
    *,
    project: str,
    start_date: str,
    end_date: str,
    usernames: Sequence[str],
    log=None,
) -> List[Dict[str, Any]]:
    authors = ", ".join(usernames)
    jql = (
        f"project = {project} AND worklogDate >= \"{start_date}\" "
        f"AND worklogDate <= \"{end_date}\" "
        f"AND worklogAuthor in ({authors})"
    )
    if log:
        print(f"Списания {start_date}…{end_date}…", file=log)
    raw_issues = issues_service.search_issues_by_jql(
        client,
        jql,
        fields="summary,status,assignee,issuetype,worklog",
        expand="",
    )
    if log:
        print(f"  задач со списаниями: {len(raw_issues)}", file=log)

    start_day = date.fromisoformat(start_date)
    end_day = date.fromisoformat(end_date)
    entries: List[Dict[str, Any]] = []
    seen = set()

    for issue in raw_issues:
        key = issue["key"]
        items: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            data = client.get(
                f"/rest/api/2/issue/{key}/worklog",
                params={"startAt": start_at, "maxResults": 100},
            )
            batch = data.get("worklogs") or []
            items.extend(batch)
            total = data.get("total", len(items))
            if start_at + len(batch) >= total or not batch:
                break
            start_at += len(batch)

        for wl in items:
            author = (wl.get("author") or {}).get("name") or ""
            if author not in TEAM:
                continue
            started_raw = wl.get("started") or ""
            if not started_raw:
                continue
            started = parse_jira_datetime(started_raw)
            started_day = started.date()
            if started_day < start_day or started_day > end_day:
                continue
            hours = seconds_to_hours(int(wl.get("timeSpentSeconds") or 0)) or 0.0
            wl_id = str(wl.get("id") or "")
            dedupe = (author, key, wl_id)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            entries.append(
                {
                    "username": author,
                    "key": key,
                    "hours": hours,
                    "started": started.isoformat(timespec="seconds"),
                    "date": started_day.isoformat(),
                }
            )
    return entries


def _sprint_people_from_issues(
    issues: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    people = {username: _empty_person_bucket(username) for username in TEAM}
    for issue in issues:
        username = _username(issue)
        if username not in TEAM:
            continue
        if not _counts_for_person(username, issue):
            continue
        _add_issue_to_bucket(people[username], issue)
    return people


def _apply_worklogs(
    people: Dict[str, Dict[str, Any]],
    entries: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
) -> None:
    issue_sets = {username: set() for username in TEAM}
    for entry in entries:
        if entry["date"] < start_date or entry["date"] > end_date:
            continue
        username = entry["username"]
        people[username]["worklog_hours"] += entry["hours"]
        issue_sets[username].add(entry["key"])
    for username, keys in issue_sets.items():
        people[username]["worklog_issues"] = len(keys)


def _direction_rollups(
    people: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    by_dir: Dict[str, Dict[str, Any]] = {}
    for person in people.values():
        direction = person["direction"]
        row = by_dir.setdefault(
            direction,
            {
                "direction": direction,
                "assigned": 0,
                "done": 0,
                "dev_reached": 0,
                "estimate_assigned": 0.0,
                "estimate_done": 0.0,
                "estimate_dev_reached": 0.0,
                "worklog_hours": 0.0,
                "people": [],
            },
        )
        row["assigned"] += person["assigned"]
        row["done"] += person["done"]
        row["dev_reached"] += person["dev_reached"]
        row["estimate_assigned"] += person["estimate_assigned"]
        row["estimate_done"] += person["estimate_done"]
        row["estimate_dev_reached"] += person["estimate_dev_reached"]
        row["worklog_hours"] += person["worklog_hours"]
        row["people"].append(person["short"])
    for row in by_dir.values():
        for field in (
            "estimate_assigned",
            "estimate_done",
            "estimate_dev_reached",
            "worklog_hours",
        ):
            row[field] = round(float(row[field]), 1)
    return by_dir


def _planning_hint(person_sprints: List[Dict[str, Any]], calendar_hours: float) -> Dict[str, Any]:
    closed = [row for row in person_sprints if row.get("sprint_state") == "closed"]
    source = closed or person_sprints
    worklogs = [row["worklog_hours"] for row in source]
    done_est = [row["estimate_done"] for row in source]
    done_n = [float(row["done"]) for row in source]
    reached_est = [row["estimate_dev_reached"] for row in source]
    assigned_n = [float(row["assigned"]) for row in source]

    med_wl = _median(worklogs)
    med_done_h = _median(done_est)
    med_reached_h = _median(reached_est)
    med_done_n = _median(done_n)
    med_assigned_n = _median(assigned_n)
    mean_wl = _mean(worklogs)

    wl_share = None
    if calendar_hours and med_wl is not None:
        wl_share = round(med_wl / calendar_hours, 2)

    worklog_thin = bool(wl_share is not None and wl_share < 0.35)

    plan_hours = med_reached_h if med_reached_h else med_done_h
    if med_wl and not worklog_thin:
        if plan_hours:
            plan_hours = round((plan_hours + med_wl) / 2, 1)
        else:
            plan_hours = med_wl

    note_parts = []
    if worklog_thin:
        note_parts.append("списания неполные — ориентир по задачам, не по worklog")
    if med_done_h is not None and med_reached_h is not None and med_reached_h > med_done_h * 1.3:
        note_parts.append("много доходит до теста, но не до Done в том же спринте")

    return {
        "plan_hours": plan_hours,
        "plan_done_tasks": med_done_n,
        "plan_assigned_tasks": med_assigned_n,
        "median_worklog_hours": med_wl,
        "median_done_hours": med_done_h,
        "median_dev_reached_hours": med_reached_h,
        "mean_worklog_hours": mean_wl,
        "worklog_vs_calendar": wl_share,
        "worklog_thin": worklog_thin,
        "note": "; ".join(note_parts) or None,
    }


def build_capacity_report(
    client: JiraClient,
    *,
    project: str,
    board_id: Optional[str] = None,
    closed_count: int = 4,
    log=None,
) -> Dict[str, Any]:
    resolved_board = sprints_service.find_board_id(client, project, board_id)
    if log:
        print("Ищу спринты CAT2…", file=log)
    sprints = select_sprints(client, resolved_board, closed_count=closed_count)
    if not sprints:
        raise RuntimeError("Не найдены датированные спринты CAT2")

    dates = []
    for sprint in sprints:
        start, end = sprint_date_fragment(sprint.get("startDate")), sprint_date_fragment(
            sprint.get("endDate")
        )
        if start != "unknown":
            dates.append(start)
        if end != "unknown":
            dates.append(end)
    period_start, period_end = min(dates), max(dates)

    if log:
        print("Гружу списания за период…", file=log)
    worklog_entries = fetch_worklogs_for_period(
        client,
        project=project,
        start_date=period_start,
        end_date=period_end,
        usernames=list(TEAM),
        log=log,
    )

    sprint_rows: List[Dict[str, Any]] = []
    for sprint in sprints:
        if log:
            print(f"Спринт {sprint.get('name')}…", file=log)
        raw = issues_service.get_sprint_issues(
            client, int(sprint["id"]), fields=ISSUE_FIELDS
        )
        prefix = f"{project}-"
        normalized = [
            normalize_issue(issue, client.base_url)
            for issue in raw
            if (issue.get("key") or "").startswith(prefix)
        ]
        people = _sprint_people_from_issues(normalized)
        start = sprint_date_fragment(sprint.get("startDate"))
        end = sprint_date_fragment(sprint.get("endDate"))
        _apply_worklogs(people, worklog_entries, start, end)
        for bucket in people.values():
            _round_bucket(bucket)

        workdays = _workdays(sprint)
        calendar_hours = round(workdays * HOURS_PER_WORKDAY, 1)
        sprint_rows.append(
            {
                "id": sprint.get("id"),
                "name": sprint.get("name"),
                "state": sprint.get("state"),
                "start": start,
                "end": end,
                "workdays": workdays,
                "calendar_hours": calendar_hours,
                "issue_count": len(normalized),
                "people": people,
                "directions": _direction_rollups(people),
            }
        )

    people_summary: Dict[str, Any] = {}
    typical_calendar = sprint_rows[-1]["calendar_hours"] if sprint_rows else 80.0
    for username, meta in TEAM.items():
        per_sprint = []
        for row in sprint_rows:
            person = row["people"][username]
            per_sprint.append(
                {
                    "sprint": row["name"],
                    "sprint_state": row["state"],
                    "assigned": person["assigned"],
                    "done": person["done"],
                    "dev_reached": person["dev_reached"],
                    "estimate_assigned": person["estimate_assigned"],
                    "estimate_done": person["estimate_done"],
                    "estimate_dev_reached": person["estimate_dev_reached"],
                    "worklog_hours": person["worklog_hours"],
                    "worklog_issues": person["worklog_issues"],
                }
            )
        people_summary[username] = {
            **meta,
            "username": username,
            "sprints": per_sprint,
            "planning": _planning_hint(per_sprint, typical_calendar),
        }

    direction_summary: Dict[str, Any] = {}
    for direction in sorted({meta["direction"] for meta in TEAM.values()}):
        per_sprint = []
        for row in sprint_rows:
            drow = row["directions"].get(
                direction,
                {
                    "assigned": 0,
                    "done": 0,
                    "dev_reached": 0,
                    "estimate_assigned": 0.0,
                    "estimate_done": 0.0,
                    "estimate_dev_reached": 0.0,
                    "worklog_hours": 0.0,
                },
            )
            per_sprint.append(
                {
                    "sprint": row["name"],
                    "sprint_state": row["state"],
                    **{k: drow[k] for k in (
                        "assigned",
                        "done",
                        "dev_reached",
                        "estimate_assigned",
                        "estimate_done",
                        "estimate_dev_reached",
                        "worklog_hours",
                    )},
                }
            )
        closed = [r for r in per_sprint if r["sprint_state"] == "closed"]
        src = closed or per_sprint
        direction_summary[direction] = {
            "direction": direction,
            "sprints": per_sprint,
            "median_done_hours": _median([r["estimate_done"] for r in src]),
            "median_dev_reached_hours": _median([r["estimate_dev_reached"] for r in src]),
            "median_worklog_hours": _median([r["worklog_hours"] for r in src]),
            "median_done_tasks": _median([float(r["done"]) for r in src]),
        }

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "report_generated_at": generated_at,
        "project": project,
        "board_id": resolved_board,
        "period": {"start": period_start, "end": period_end},
        "method": {
            "done": "текущий статус Done / To Prod (не статус на дату закрытия спринта)",
            "dev_reached": "Done / To Prod / To Test / Testing / Code Review / Delivery / To Launch / To Design Review",
            "assigned": "текущий assignee; Testing считаются только у QA",
            "worklog": "списания автора в календарных датах спринта",
            "planning": "медиана по закрытым спринтам; часы — смесь дошедших до теста и worklog, если списания не тонкие",
        },
        "sprints": [
            {
                "id": row["id"],
                "name": row["name"],
                "state": row["state"],
                "start": row["start"],
                "end": row["end"],
                "workdays": row["workdays"],
                "calendar_hours": row["calendar_hours"],
                "issue_count": row["issue_count"],
            }
            for row in sprint_rows
        ],
        "sprint_details": sprint_rows,
        "people": people_summary,
        "directions": direction_summary,
        "caveats": [
            "Количество задач нельзя читать как эффективность: оценка 4ч и 18ч — разные объёмы.",
            "Worklog у части людей неполный; низкая доля от календарных 8ч/день — это про трекинг, не про простой.",
            "Статус сейчас, не на дату закрытия: старые спринты чуть завышают Done, текущий — занижает.",
            "Carry-over попадает в assigned нескольких спринтов подряд.",
        ],
    }


def _fmt_h(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}ч" if float(value).is_integer() else f"{value}ч"


def _fmt_n(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        f"# Ёмкость CAT2 — к планированию спринта",
        "",
        f"**Дата отчёта:** {report['report_generated_at']}",
        f"**Период:** {report['period']['start']} — {report['period']['end']}",
        "",
        "## Спринты",
        "",
        "| Спринт | Состояние | Раб. дни | Календарь | Задач в спринте |",
        "|--------|-----------|----------|-----------|-----------------|",
    ]
    for sprint in report["sprints"]:
        lines.append(
            f"| {sprint['name']} | {sprint['state']} | {sprint['workdays']} | "
            f"{_fmt_h(sprint['calendar_hours'])} | {sprint['issue_count']} |"
        )

    lines += [
        "",
        "## Ориентир на человека (медиана закрытых спринтов)",
        "",
        "| Кто | Направление | Часы в план | Задач Done | Задач в спринте | Worklog (мед.) | Списания |",
        "|-----|-------------|-------------|------------|-----------------|----------------|----------|",
    ]
    order = [
        "kafanova_m",
        "goldebaev_a",
        "guminichenko_d",
        "gabisov_r",
        "pushkarev_a",
        "gromazina_o",
        "lebedev_m",
        "filetkina_a",
        "cherkasov_ko",
    ]
    for username in order:
        person = report["people"][username]
        plan = person["planning"]
        thin = "тонкие" if plan.get("worklog_thin") else "ок"
        lines.append(
            f"| {person['short']} | {person['direction']} | "
            f"{_fmt_h(plan.get('plan_hours'))} | "
            f"{_fmt_n(plan.get('plan_done_tasks'))} | "
            f"{_fmt_n(plan.get('plan_assigned_tasks'))} | "
            f"{_fmt_h(plan.get('median_worklog_hours'))} | {thin} |"
        )

    lines += ["", "### По спринтам"]
    for username in order:
        person = report["people"][username]
        lines += [
            "",
            f"#### {person['short']} ({person['direction']})",
            "",
            "| Спринт | В спринте | Done | До теста+ | Оценка Done | Оценка до теста+ | Worklog |",
            "|--------|-----------|------|-----------|-------------|------------------|---------|",
        ]
        for row in person["sprints"]:
            lines.append(
                f"| {row['sprint']} | {row['assigned']} | {row['done']} | "
                f"{row['dev_reached']} | {_fmt_h(row['estimate_done'])} | "
                f"{_fmt_h(row['estimate_dev_reached'])} | {_fmt_h(row['worklog_hours'])} |"
            )
        note = person["planning"].get("note")
        if note:
            lines.append(f"\n_{note}_")

    lines += [
        "",
        "## По направлениям (медиана закрытых)",
        "",
        "| Направление | Done, ч | До теста+, ч | Worklog, ч | Done, задач |",
        "|-------------|---------|--------------|------------|-------------|",
    ]
    for direction in ("SA", "Backend", "Web", "Android", "iOS", "QA"):
        row = report["directions"].get(direction)
        if not row:
            continue
        lines.append(
            f"| {direction} | {_fmt_h(row['median_done_hours'])} | "
            f"{_fmt_h(row['median_dev_reached_hours'])} | "
            f"{_fmt_h(row['median_worklog_hours'])} | "
            f"{_fmt_n(row['median_done_tasks'])} |"
        )

    lines += ["", "## Ограничения метода", ""]
    for caveat in report.get("caveats") or []:
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


def save_report(
    report: Dict[str, Any],
    *,
    output_format: str = "both",
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    directory = reports_dir or DEFAULT_REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    when = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M")
    start = report["period"]["start"]
    end = report["period"]["end"]
    stem = f"{when}__{report['project']}__sprint-capacity__{start}__{end}"
    paths: Dict[str, Path] = {}
    if output_format in {"json", "both"}:
        json_path = directory / f"{stem}.json"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths["json"] = json_path
    if output_format in {"markdown", "both"}:
        md_path = directory / f"{stem}.md"
        if "json" in paths:
            md_path = paths["json"].with_suffix(".md")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path
    return paths


def print_text_summary(report: Dict[str, Any], stream=None) -> None:
    import sys

    out = stream or sys.stdout
    print(f"Период: {report['period']['start']} — {report['period']['end']}", file=out)
    print("Ориентир на человека:", file=out)
    for username in (
        "kafanova_m",
        "goldebaev_a",
        "guminichenko_d",
        "gabisov_r",
        "pushkarev_a",
        "gromazina_o",
        "lebedev_m",
        "filetkina_a",
        "cherkasov_ko",
    ):
        person = report["people"][username]
        plan = person["planning"]
        print(
            f"  {person['short']:7} {person['direction']:8} "
            f"план {_fmt_h(plan.get('plan_hours')):>6}  "
            f"done-задач {_fmt_n(plan.get('plan_done_tasks')):>4}  "
            f"worklog {_fmt_h(plan.get('median_worklog_hours')):>6}",
            file=out,
        )
