"""Build and persist normalized Jira issue / search reports."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def build_issue_report(*, issue: Dict[str, Any], query: str) -> Dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "report_generated_at": generated_at,
        "query_type": "issue",
        "query": query,
        "total_issues": 1,
        "issues": [issue],
    }


def build_search_report(*, jql: str, issues: List[Dict[str, Any]], limit: Optional[int]) -> Dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "report_generated_at": generated_at,
        "query_type": "search",
        "query": jql,
        "limit": limit,
        "total_issues": len(issues),
        "issues": issues,
    }


def report_basename(report: Dict[str, Any], generated_at: Optional[datetime] = None) -> str:
    when = generated_at or datetime.now().astimezone()
    ts = when.strftime("%Y-%m-%d_%H-%M")
    query_type = report.get("query_type", "search")
    query = report.get("query", "")

    if query_type == "issue":
        slug = _slugify(query or "issue")
    else:
        slug = _slugify(query or "search")
    return f"{ts}__jira__{query_type}__{slug}"


def save_report(
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
        md_path = directory / f"{stem}.md"
        if "json" in paths:
            md_path = paths["json"].with_suffix(".md")
        else:
            md_path = _unique_path(md_path)
        md_path.write_text(render_markdown(report), encoding="utf-8")
        paths["markdown"] = md_path

    return paths


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        f"# Jira {report.get('query_type', 'search')}",
        "",
        f"**Дата отчёта:** {report.get('report_generated_at', '')}",
        f"**Запрос:** `{report.get('query', '')}`",
        f"**Найдено задач:** {report.get('total_issues', 0)}",
        "",
    ]

    for issue in report.get("issues", []):
        assignee = issue.get("assignee") or "Не назначен"
        status = issue.get("status") or "Без статуса"
        issue_type = issue.get("type") or "Без типа"
        lines.append(
            f"- [{issue['key']}]({issue['url']}) — [{status}] [{issue_type}] {issue['title']} ({assignee})"
        )

    lines.append("")
    return "\n".join(lines)


def print_text_summary(report: Dict[str, Any]) -> None:
    print(f"Тип запроса: {report.get('query_type')}")
    print(f"Запрос: {report.get('query')}")
    print(f"Найдено задач: {report.get('total_issues')}")
    print()

    for issue in report.get("issues", [])[:20]:
        assignee = issue.get("assignee") or "Не назначен"
        status = issue.get("status") or "Без статуса"
        print(f"- {issue['key']} [{status}] {issue['title']} ({assignee})")

    remainder = max(0, report.get("total_issues", 0) - 20)
    if remainder:
        print(f"\n... ещё {remainder} задач(и) в JSON/Markdown.")


def _slugify(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned[:80] or "report"


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
