#!/usr/bin/env python3
"""CLI: monitor To Test queue (old/new flow) and suggest sprint placement."""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from reports.testing_monitor import (  # noqa: E402
    build_testing_monitor,
    load_monitor_config,
    print_testing_monitor_summary,
    save_testing_monitor_report,
)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description=(
            "Мониторинг очереди тестирования: To Test, old/new flow, "
            "ёмкость спринта (без записи в Jira)"
        )
    )
    parser.add_argument("--project", default=None, help="Ключ проекта (по умолчанию из .env)")
    parser.add_argument(
        "--config",
        default=None,
        help="Путь к testing_monitor.yaml (по умолчанию scripts/config/testing_monitor.yaml)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="Что сохранить (по умолчанию both)",
    )
    args = parser.parse_args()

    try:
        config = load_config(project_override=args.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    config_path = Path(args.config) if args.config else None
    try:
        monitor_config = load_monitor_config(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка конфига: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            report = build_testing_monitor(
                client,
                config,
                monitor_config=monitor_config,
            )
            paths = save_testing_monitor_report(report, output_format=args.format)
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print_testing_monitor_summary(report)
    print()
    for kind, path in paths.items():
        print(f"Сохранено ({kind}): {path}")


if __name__ == "__main__":
    main()
