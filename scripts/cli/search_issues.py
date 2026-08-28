#!/usr/bin/env python3
"""CLI: search Jira issues by JQL and save normalized report."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from models.issue import normalize_issue  # noqa: E402
from reports.issue_search import (  # noqa: E402
    build_search_report,
    print_text_summary,
    save_report,
)
from services import issues as issues_service  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Найти задачи Jira по JQL и сохранить нормализованный JSON/Markdown"
    )
    parser.add_argument("--jql", required=True, help="JQL-запрос, например project = CAT2")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Максимум задач в выдаче; без параметра забираются все страницы",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="Что сохранить в reports/ (по умолчанию both)",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            raw_issues = issues_service.search_issues_by_jql(
                client,
                args.jql,
                limit=args.limit,
            )
            normalized = [normalize_issue(issue, client.base_url) for issue in raw_issues]
            report = build_search_report(jql=args.jql, issues=normalized, limit=args.limit)
            paths = save_report(report, output_format=args.format)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print_text_summary(report)
    print()
    for kind, path in paths.items():
        print(f"Сохранено ({kind}): {path}")


if __name__ == "__main__":
    main()
