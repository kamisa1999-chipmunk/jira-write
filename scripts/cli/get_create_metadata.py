#!/usr/bin/env python3
"""CLI: dump create/edit metadata for a Jira project (issue types, fields, links)."""

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
from services import metadata as meta  # noqa: E402


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

    parser = argparse.ArgumentParser(
        description="Метаданные создания задач Jira (типы, поля, приоритеты, связи)"
    )
    parser.add_argument(
        "--project",
        help="Ключ проекта (по умолчанию из .env)",
    )
    parser.add_argument(
        "--issue-type",
        help="Если указан — загрузить поля только для этого типа",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Загрузить поля для всех типов (дольше)",
    )
    args = parser.parse_args()

    try:
        config = load_config(project_override=args.project)
        project_config = load_project_config(config.project)
    except JiraConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(config) as client:
            issue_types = meta.list_create_issue_types(client, config.project)
            fields_by_type = {}

            selected = issue_types
            if args.issue_type:
                found = meta.find_issue_type(issue_types, args.issue_type)
                if not found:
                    names = ", ".join(t.get("name") or "?" for t in issue_types)
                    print(
                        f"Ошибка: тип {args.issue_type!r} не найден. Доступны: {names}",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                selected = [found]
            elif not args.full:
                # Default: common non-subtask types only (faster)
                preferred = {
                    "Bug",
                    "DevelopmentB",
                    "DevelopmentF",
                    "Testing",
                    "Analysis",
                    "Design",
                    "Specification",
                    "Epic",
                }
                selected = [
                    t for t in issue_types if t.get("name") in preferred
                ] or [t for t in issue_types if not t.get("subtask")][:8]

            for issue_type in selected:
                type_id = str(issue_type["id"])
                fields_by_type[type_id] = meta.get_create_fields(
                    client, config.project, type_id
                )

            result = meta.summarize_create_metadata(
                project_key=config.project,
                issue_types=selected if (args.issue_type or not args.full) else issue_types,
                fields_by_type=fields_by_type,
                priorities=meta.get_priorities(client),
                components=meta.get_components(client, config.project),
                versions=meta.get_versions(client, config.project),
                link_types=meta.get_link_types(client),
                project_config=project_config,
            )
            result["all_issue_types"] = [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "subtask": bool(t.get("subtask")),
                }
                for t in issue_types
            ]
    except JiraError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Сетевая или внутренняя ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
