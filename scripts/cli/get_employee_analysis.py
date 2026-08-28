#!/usr/bin/env python3
"""CLI: analyze employee's Jira work for a period and save a structured report."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config.project_config import load_project_config  # noqa: E402
from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from reports.employee_analysis import (  # noqa: E402
    build_employee_analysis,
    parse_period,
    print_employee_summary,
    save_employee_report,
)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Анализ работы сотрудника в Jira за период (с учётом истории assignee)"
    )
    parser.add_argument(
        "--employee",
        required=True,
        help="Имя / фамилия / username / алиас (маша, андрей, …)",
    )
    parser.add_argument("--project", default=None, help="Ключ проекта (по умолчанию из .env)")
    parser.add_argument("--from", dest="date_from", help="Начало периода YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="Конец периода YYYY-MM-DD")
    parser.add_argument("--months", type=int, help="Последние N месяцев (≈30*N дней)")
    parser.add_argument("--month", help="Календарный месяц YYYY-MM")
    parser.add_argument("--quarter", help="Квартал, например 2026-Q2")
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="Что сохранить (по умолчанию both)",
    )
    parser.add_argument(
        "--with-git",
        action="store_true",
        help="Полный Git-анализ связанных MR (только после подтверждения пользователя)",
    )
    parser.add_argument(
        "--no-mr-discovery",
        action="store_true",
        help="Не искать связанные MR через Jira (быстрее, без git-offer)",
    )
    args = parser.parse_args()

    try:
        period_start, period_end, period_label = parse_period(
            date_from=args.date_from,
            date_to=args.date_to,
            months=args.months,
            month=args.month,
            quarter=args.quarter,
        )
    except ValueError as exc:
        print(f"Ошибка периода: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config(project_override=args.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    project_config = load_project_config(config.project)

    git_client = None
    git_client_cm = None
    if args.with_git:
        try:
            from git_client import (  # noqa: WPS433
                GitConfigError,
                create_git_client,
                load_git_config,
            )
        except ImportError as exc:
            print(f"Ошибка импорта git_client: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            git_config = load_git_config()
            git_client_cm = create_git_client(git_config)
            git_client = git_client_cm.__enter__()
        except GitConfigError as exc:
            print(f"Ошибка Git-конфига: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        with JiraClient(config) as client:
            report = build_employee_analysis(
                client,
                employee_query=args.employee,
                period_start=period_start,
                period_end=period_end,
                period_label=period_label,
                project_key=config.project,
                project_config=project_config,
                discover_mrs=not args.no_mr_discovery,
                with_git=args.with_git,
                git_client=git_client,
            )
            paths = save_employee_report(report, output_format=args.format)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if git_client_cm is not None:
            git_client_cm.__exit__(None, None, None)

    print_employee_summary(report)
    print()
    for kind, path in paths.items():
        print(f"Сохранено ({kind}): {path}")


if __name__ == "__main__":
    main()
