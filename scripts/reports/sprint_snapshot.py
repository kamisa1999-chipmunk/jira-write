"""Build and persist active-sprint snapshot reports."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jira_client import JiraClient, JiraConfig
from models.issue import CATEGORY_LABELS, normalize_issue
from models.sprint import normalize_sprint, sprint_date_fragment
from services import issues as issues_service
from services import sprints as sprints_service

# Default reports dir: reports/
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def build_sprint_snapshot(
    client: JiraClient,
    config: JiraConfig,
    *,
    sprint_id: Optional[int] = None,
    with_timing: bool = True,
    progress_stream=sys.stderr,
) -> Dict[str, Any]:
    """Fetch active (or specified) sprint, issues, optional changelogs."""
    log = progress_stream

    print("Подключаюсь к Jira...", file=log)
    server_info = client.get_server_info()
    print(
        f"Jira {server_info.get('version', '?')}, проект {config.project}",
        file=log,
    )

    print("Ищу доску и активный спринт...", file=log)
    board_id = sprints_service.find_board_id(
        client, config.project, config.board_id or None
    )
    if sprint_id is None:
        sprint_short = sprints_service.get_active_sprint(client, board_id)
        sprint_id = int(sprint_short["id"])
    sprint_raw = sprints_service.get_sprint_details(client, int(sprint_id))
    sprint = normalize_sprint(sprint_raw)
    print(f"Спринт: {sprint.get('name')}", file=log)

    print("Загружаю задачи спринта...", file=log)
    raw_issues = issues_service.get_sprint_issues(client, int(sprint["id"]))
    print(f"Найдено задач: {len(raw_issues)}", file=log)

    # Keep only issues whose key belongs to the requested project
    # (sprint board may include linked issues from other projects).
    project_prefix = f"{config.project}-"
    project_issues = [
        issue
        for issue in raw_issues
        if issue.get("key", "").startswith(project_prefix)
    ]

    timing_targets = [
        issue
        for issue in project_issues
        if (issue.get("fields", {}).get("status", {}) or {}).get("name")
        not in {"Done", "Canceled"}
    ]
    if with_timing and timing_targets:
        print(
            f"Анализирую оценки и риски ({len(timing_targets)} задач)...",
            file=log,
        )

    normalized: List[Dict[str, Any]] = []
    for issue in project_issues:
        status = (issue.get("fields", {}).get("status", {}) or {}).get("name")
        changelog = None
        if with_timing and status not in {"Done", "Canceled"}:
            changelog = issues_service.get_issue_changelog(client, issue["key"])
        normalized.append(normalize_issue(issue, client.base_url, changelog))

    return _assemble_report(
        server_info=server_info,
        project=config.project,
        board_id=board_id,
        sprint=sprint,
        normalized=normalized,
    )


def _assemble_report(
    *,
    server_info: Dict[str, Any],
    project: str,
    board_id: int,
    sprint: Dict[str, Any],
    normalized: List[Dict[str, Any]],
) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    by_assignee: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    by_platform: Dict[str, int] = {}
    unassigned = 0
    no_estimate = 0
    risk_issues: List[Dict[str, Any]] = []

    for issue in normalized:
        status = issue["status"] or "Без статуса"
        assignee = issue["assignee"] or "Не назначен"
        category = issue["status_category"] or "other"
        platform = issue.get("platform") or "Без платформы"

        by_status[status] = by_status.get(status, 0) + 1
        by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
        by_category[category] = by_category.get(category, 0) + 1
        by_platform[platform] = by_platform.get(platform, 0) + 1

        if not issue["assignee"]:
            unassigned += 1
        # TODO: closed/delivery excluded from "without estimate" as in legacy;
        # ready_to_prod still counted if estimate is missing.
        if issue["estimate_hours"] is None and category not in {"closed", "delivery"}:
            no_estimate += 1
        if issue["risks"]:
            risk_issues.append(issue)

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    return {
        "report_generated_at": generated_at,
        "jira_version": server_info.get("version"),
        "project": project,
        "board_id": board_id,
        "sprint": sprint,
        "summary": {
            "total_issues": len(normalized),
            "by_status": by_status,
            "by_assignee": by_assignee,
            "by_platform": by_platform,
            "by_category": by_category,
            "unassigned": unassigned,
            "without_estimate": no_estimate,
            "risk_count": len(risk_issues),
            "category_labels": dict(CATEGORY_LABELS),
        },
        "risks": risk_issues,
        "issues": normalized,
    }


def report_basename(report: Dict[str, Any], generated_at: Optional[datetime] = None) -> str:
    """Filename stem: {ts}__{PROJECT}__sprint-{id}__{start}__{end}."""
    when = generated_at or datetime.now().astimezone()
    ts = when.strftime("%Y-%m-%d_%H-%M")
    sprint = report["sprint"]
    project = report["project"]
    start = sprint_date_fragment(sprint.get("startDate"))
    end = sprint_date_fragment(sprint.get("endDate"))
    return f"{ts}__{project}__sprint-{sprint['id']}__{start}__{end}"


def render_markdown(report: Dict[str, Any]) -> str:
    sprint = report["sprint"]
    summary = report["summary"]
    labels = summary.get("category_labels", {})
    generated = report.get("report_generated_at", "")

    lines = [
        f"# Спринт {sprint.get('name')} — {report['project']}",
        "",
        f"**Дата отчёта:** {generated}",
        f"**Период:** {sprint.get('startDate', '?')} — {sprint.get('endDate', '?')}",
        f"**Задач:** {summary['total_issues']}",
        f"**Снимок на момент формирования** (id спринта: {sprint.get('id')})",
        "",
    ]

    goal = (sprint.get("goal") or "").strip()
    if goal:
        lines.extend(["## Цели спринта", "", goal, ""])

    lines.extend(["## По категориям", "", "| Категория | Кол-во |", "|-----------|--------|"])
    for category, count in sorted(summary["by_category"].items()):
        lines.append(f"| {labels.get(category, category)} | {count} |")

    lines.extend(
        [
            "",
            f"**Без исполнителя:** {summary['unassigned']}",
            f"**Без оценки (ч):** {summary['without_estimate']}",
            f"**С рисками:** {summary['risk_count']}",
            "",
        ]
    )

    if report.get("risks"):
        lines.extend(["## Риски", ""])
        for issue in report["risks"][:30]:
            risks = ",".join(issue["risks"])
            lines.append(
                f"- [{issue['key']}]({issue['url']}) — [{issue['status']}] "
                f"`{risks}`: {issue['summary']}"
            )
        lines.append("")

    lines.extend(["## По статусам", "", "| Статус | Кол-во |", "|--------|--------|"])
    for status, count in sorted(summary["by_status"].items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {status} | {count} |")

    if summary.get("by_platform"):
        lines.extend(
            ["", "## По платформам", "", "| Платформа | Кол-во |", "|-----------|--------|"]
        )
        for platform, count in sorted(
            summary["by_platform"].items(), key=lambda x: (-x[1], x[0])
        ):
            lines.append(f"| {platform} | {count} |")

    lines.extend(
        ["", "## По исполнителям", "", "| Исполнитель | Кол-во |", "|-------------|--------|"]
    )
    for assignee, count in sorted(
        summary["by_assignee"].items(), key=lambda x: (-x[1], x[0])
    ):
        lines.append(f"| {assignee} | {count} |")

    lines.append("")
    return "\n".join(lines)


def save_report(
    report: Dict[str, Any],
    *,
    output_format: str = "both",
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """Write new timestamped files; never overwrite existing reports.

    output_format: json | markdown | both
    """
    directory = reports_dir or DEFAULT_REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    stem = report_basename(report)
    paths: Dict[str, Path] = {}

    if output_format in {"json", "both"}:
        json_path = directory / f"{stem}.json"
        # Extremely unlikely collision within the same minute; bump minutes if needed.
        json_path = _unique_path(json_path)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths["json"] = json_path

    if output_format in {"markdown", "both"}:
        md_path = directory / f"{stem}.md"
        if "json" in paths:
            md_path = paths["json"].with_suffix(".md")
        else:
            md_path = _unique_path(md_path)
        md_path.write_text(render_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path

    return paths


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    # Same-minute collision: append -2, -3, ...
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def print_text_summary(report: Dict[str, Any]) -> None:
    sprint = report["sprint"]
    summary = report["summary"]

    print(f"Проект: {report['project']}")
    print(f"Спринт: {sprint['name']} (id={sprint['id']}, state={sprint['state']})")
    if sprint.get("goal"):
        print(f"\nЦели спринта:\n{sprint['goal'].strip()}\n")
    if sprint.get("startDate") or sprint.get("endDate"):
        print(f"Период: {sprint.get('startDate', '?')} — {sprint.get('endDate', '?')}")
    print(f"Задач в спринте: {summary['total_issues']}")

    print("\nПо категориям:")
    labels = summary.get("category_labels", {})
    for category, count in sorted(summary["by_category"].items()):
        print(f"  - {labels.get(category, category)}: {count}")

    print(f"\nБез исполнителя: {summary['unassigned']}")
    print(f"Без оценки (ч): {summary['without_estimate']}")
    print(f"С рисками: {summary['risk_count']}")

    if report.get("risks"):
        print("\nРиски (топ 10):")
        for issue in report["risks"][:10]:
            print(
                f"  - {issue['key']} [{issue['status']}] "
                f"риски={','.join(issue['risks'])}: {issue['summary']}"
            )
