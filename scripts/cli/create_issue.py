#!/usr/bin/env python3
"""CLI: preview or create Jira issue(s) from JSON input."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Dict

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from services import issue_write  # noqa: E402


def _load_input(path: str) -> Dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Создать задачу Jira из JSON (по умолчанию только preview)"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Путь к JSON или '-' для stdin",
    )
    parser.add_argument(
        "--project",
        help="Ключ проекта (если не указан в JSON / .env)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально создать задачу (без флага — только preview)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Режим пакета: ожидает {issues:[...], links:[...]}",
    )
    args = parser.parse_args()

    try:
        payload = _load_input(args.input)
        config = load_config(project_override=args.project)
    except (JiraConfigError, json.JSONDecodeError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            is_batch = args.batch or isinstance(payload.get("issues"), list)
            if is_batch:
                preview = issue_write.build_batch_create_preview(
                    client,
                    payload,
                    project_key=args.project or config.project,
                    board_id=config.board_id or None,
                )
                if args.apply:
                    if not preview.get("ready"):
                        print(json.dumps(preview, ensure_ascii=False, indent=2))
                        print(
                            "Ошибка: пакет не ready — исправь missing/unresolved",
                            file=sys.stderr,
                        )
                        sys.exit(2)
                    result = issue_write.apply_batch_create(client, preview)
                else:
                    result = preview
            else:
                preview = issue_write.build_create_preview(
                    client,
                    payload,
                    project_key=args.project or config.project,
                    board_id=config.board_id or None,
                )
                if args.apply:
                    if not preview.get("ready"):
                        print(json.dumps(preview, ensure_ascii=False, indent=2))
                        print(
                            "Ошибка: preview не ready — исправь missing/unresolved",
                            file=sys.stderr,
                        )
                        sys.exit(2)
                    result = issue_write.apply_create(client, preview)
                else:
                    result = preview
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.apply and not result.get("ok", True):
        sys.exit(3)


if __name__ == "__main__":
    main()
