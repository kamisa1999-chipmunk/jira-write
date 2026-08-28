#!/usr/bin/env python3
"""CLI: fetch one Jira issue and save normalized report."""

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
    build_issue_report,
    print_text_summary,
    save_report,
)
from services import issues as issues_service  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Получить одну задачу Jira и сохранить нормализованный JSON/Markdown"
    )
    parser.add_argument("issue_key", help="Ключ задачи, например CAT2-1234")
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
            raw_issue = issues_service.get_issue_details(client, args.issue_key)
            normalized = normalize_issue(raw_issue, client.base_url)
            report = build_issue_report(issue=normalized, query=args.issue_key)
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
