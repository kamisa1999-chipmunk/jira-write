#!/usr/bin/env python3
"""CLI: preview or create a link between two Jira issues."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config.project_config import load_project_config  # noqa: E402
from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from services import issue_write  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Связать две задачи Jira (по умолчанию только preview)"
    )
    parser.add_argument("source", help="Ключ исходной задачи")
    parser.add_argument("target", help="Ключ целевой задачи")
    parser.add_argument(
        "--relation",
        default="relates",
        help="Тип связи: blocks / relates / duplicates / … (по умолчанию relates)",
    )
    parser.add_argument(
        "--direction",
        choices=("outward", "inward"),
        default="outward",
        help="Направление: outward = source→target по outward-имени типа",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально создать связь",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            project_config = load_project_config(config.project)
            result = issue_write.create_link(
                client,
                source_key=args.source,
                target_key=args.target,
                relation=args.relation,
                direction=args.direction,
                project_config=project_config,
                apply=args.apply,
            )
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
