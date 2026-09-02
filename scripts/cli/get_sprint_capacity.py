#!/usr/bin/env python3
"""CLI: capacity across recent CAT2 sprints for planning."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from reports.sprint_capacity import (  # noqa: E402
    build_capacity_report,
    print_text_summary,
    save_report,
)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Ёмкость команды CAT2 по последним спринтам → JSON/Markdown"
    )
    parser.add_argument("--project", help="Ключ проекта (по умолчанию из .env)")
    parser.add_argument(
        "--closed",
        type=int,
        default=4,
        help="Сколько закрытых спринтов CAT2 взять (плюс активный)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
    )
    args = parser.parse_args()

    try:
        config = load_config(project_override=args.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            report = build_capacity_report(
                client,
                project=config.project,
                board_id=config.board_id or None,
                closed_count=args.closed,
                log=sys.stderr,
            )
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
