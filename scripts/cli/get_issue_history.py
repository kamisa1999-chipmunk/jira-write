#!/usr/bin/env python3
"""CLI: fetch Jira issue history and save a readable timeline report."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from git_client import (  # noqa: E402
    GitConfigError,
    GitError,
    create_git_client,
    load_git_config,
)
from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from models.issue import normalize_issue  # noqa: E402
from reports.issue_history import (  # noqa: E402
    build_history_report,
    print_history_summary,
    save_history_report,
)
from reports.mr_analysis import attach_mr_analysis  # noqa: E402
from services import issues as issues_service  # noqa: E402
from services.merge_requests import collect_merge_requests  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Собрать историю задачи Jira (changelog + комментарии) и сохранить отчёт"
    )
    parser.add_argument("issue_key", help="Ключ задачи, например CAT2-1234")
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="Что сохранить в reports/history/ (по умолчанию both)",
    )
    parser.add_argument(
        "--with-git",
        action="store_true",
        help="Дополнительно найти связанные MR/PR через GitLab/GitHub API",
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
            report = build_history_report(normalized)

            if args.with_git:
                report = _attach_git_analysis(
                    report,
                    issue_key=args.issue_key,
                    raw_issue=raw_issue,
                    jira_client=client,
                )

            paths = save_history_report(report, output_format=args.format)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print_history_summary(report)
    print()
    for kind, path in paths.items():
        print(f"Сохранено ({kind}): {path}")


def _attach_git_analysis(
    report: dict,
    *,
    issue_key: str,
    raw_issue: dict,
    jira_client: JiraClient,
) -> dict:
    """Best-effort MR analysis: Jira history always remains usable."""
    try:
        git_config = load_git_config()
    except GitConfigError as exc:
        return attach_mr_analysis(
            report,
            {
                "ok": False,
                "provider": None,
                "reason": str(exc),
                "merge_requests": [],
            },
        )

    try:
        with create_git_client(git_config) as git_client:
            bundle = collect_merge_requests(
                issue_key=issue_key,
                raw_issue=raw_issue,
                jira_client=jira_client,
                git_client=git_client,
            )
            return attach_mr_analysis(report, bundle)
    except GitError as exc:
        return attach_mr_analysis(
            report,
            {
                "ok": False,
                "provider": git_config.provider,
                "reason": f"Git API недоступен: {exc}",
                "merge_requests": [],
            },
        )


if __name__ == "__main__":
    main()
