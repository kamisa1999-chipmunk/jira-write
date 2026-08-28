#!/usr/bin/env python3
"""CLI: preview or add a comment to a Jira issue."""

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
from services import issue_write  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Добавить комментарий к задаче Jira (по умолчанию preview)"
    )
    parser.add_argument("issue_key", help="Ключ задачи")
    parser.add_argument(
        "--text",
        help="Текст комментария",
    )
    parser.add_argument(
        "--input",
        help="Файл с текстом комментария (альтернатива --text)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально добавить комментарий",
    )
    args = parser.parse_args()

    if args.text:
        text = args.text
    elif args.input:
        text = Path(args.input).read_text(encoding="utf-8")
    else:
        print("Ошибка: нужен --text или --input", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config()
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            result = issue_write.add_comment(
                client, args.issue_key, text, apply=args.apply
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
