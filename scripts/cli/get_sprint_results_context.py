#!/usr/bin/env python3
"""CLI: collect sprint-results context (facts only, no Mattermost text)."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from reports.sprint_results_context import (  # noqa: E402
    build_sprint_results_context,
    print_text_summary,
    save_results_context,
)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description=(
            "Контекст итогов спринта CAT2: Jira + Confluence + локальные "
            "заметки. Финальный текст пишет агент, не этот скрипт."
        )
    )
    parser.add_argument(
        "--project",
        help="Ключ проекта (по умолчанию из JIRA_PROJECT в .env)",
    )
    parser.add_argument(
        "--sprint-id",
        type=int,
        help="Id спринта. Если не указан — ближайший завершённый/завершающийся",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Игнорировать кэш младше 8 часов и собрать данные заново",
    )
    args = parser.parse_args()

    try:
        config = load_config(project_override=args.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            report = build_sprint_results_context(
                client,
                config,
                sprint_id=args.sprint_id,
                refresh=args.refresh,
            )
            reused = report.get("reused_from")
            path = Path(reused) if reused else save_results_context(report)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print_text_summary(report)
    print()
    print(f"Сохранено (json): {path}")
    selection = report.get("selection") or {}
    if selection.get("status") != "chosen":
        sys.exit(2)


if __name__ == "__main__":
    main()
