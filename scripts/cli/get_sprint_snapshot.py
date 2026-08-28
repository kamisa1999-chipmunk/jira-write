#!/usr/bin/env python3
"""CLI: fetch active sprint snapshot and save report files."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

# Allow running as `python3 scripts/cli/get_sprint_snapshot.py`
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from reports.sprint_snapshot import (  # noqa: E402
    build_sprint_snapshot,
    print_text_summary,
    save_report,
)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Снимок активного спринта Jira → JSON/Markdown в reports/"
    )
    parser.add_argument(
        "--project",
        help="Ключ проекта (по умолчанию из JIRA_PROJECT в .env)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="Что сохранить в reports/ (по умолчанию both)",
    )
    parser.add_argument(
        "--sprint-id",
        type=int,
        help="Id спринта (по умолчанию текущий активный на доске)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Без changelog: быстрее, но без рисков и времени в статусе",
    )
    args = parser.parse_args()

    try:
        config = load_config(project_override=args.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            report = build_sprint_snapshot(
                client, config, sprint_id=args.sprint_id, with_timing=not args.fast
            )
            paths = save_report(report, output_format=args.format)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # network / unexpected
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print_text_summary(report)
    print()
    for kind, path in paths.items():
        print(f"Сохранено ({kind}): {path}")


if __name__ == "__main__":
    main()
