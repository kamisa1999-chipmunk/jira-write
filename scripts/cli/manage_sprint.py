#!/usr/bin/env python3
"""CLI: preview or create CAT2 sprint + Confluence page."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from confluence_client import (  # noqa: E402
    ConfluenceClient,
    ConfluenceConfigError,
    ConfluenceError,
    load_confluence_config,
)
from jira_client import JiraClient, JiraConfigError, JiraError, load_config  # noqa: E402
from services import sprint_management as sm  # noqa: E402


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_goals(args: argparse.Namespace) -> List[str]:
    if args.goals_file:
        raw = Path(args.goals_file).read_text(encoding="utf-8")
        goals: List[str] = []
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            text = text.lstrip("0123456789.).-–— ").strip()
            if text:
                goals.append(text)
        return goals
    if args.goal:
        return [g.strip() for g in args.goal if g.strip()]
    return []


def main() -> None:
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
    warnings.filterwarnings("ignore", category=Warning, module="urllib3")

    parser = argparse.ArgumentParser(
        description=(
            "Создать спринт CAT2 + страницу Confluence "
            "(по умолчанию только preview)"
        )
    )
    parser.add_argument("--start", help="Дата начала DD.MM.YY или YYYY-MM-DD")
    parser.add_argument("--end", help="Дата окончания DD.MM.YY или YYYY-MM-DD")
    parser.add_argument(
        "--goal",
        action="append",
        default=[],
        help="Цель спринта (можно несколько --goal)",
    )
    parser.add_argument(
        "--goals-file",
        help="Файл с целями (по одной на строку)",
    )
    parser.add_argument("--project", help="Ключ проекта (по умолчанию из .env)")
    parser.add_argument(
        "--config",
        help="Путь к sprint_management.yaml",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально создать спринт и страницу",
    )
    parser.add_argument(
        "--resume",
        help="JSON частичного результата для продолжения после ошибки",
    )
    parser.add_argument(
        "--preview-file",
        help="Готовый preview JSON (для --apply без пересчёта)",
    )
    parser.add_argument(
        "--verify",
        type=int,
        metavar="SPRINT_ID",
        help="Только проверить настройки существующего спринта",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "both"),
        default="both",
        help="Формат вывода",
    )
    args = parser.parse_args()

    try:
        jira_config = load_config(project_override=args.project)
        conf_config = load_confluence_config()
        mgmt_cfg = sm.load_sprint_management_config(
            Path(args.config) if args.config else None
        )
    except (JiraConfigError, ConfluenceConfigError, OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with JiraClient(jira_config) as jira, ConfluenceClient(conf_config) as conf:
            if args.verify:
                from services import sprints as sprint_svc

                detail = sprint_svc.get_sprint_details(jira, args.verify)
                dest = detail.get("incompleteIssuesDestinationId")
                kind = sm.classify_incomplete_destination(dest)
                payload = {
                    "id": detail.get("id"),
                    "name": detail.get("name"),
                    "state": detail.get("state"),
                    "startDate": detail.get("startDate"),
                    "endDate": detail.get("endDate"),
                    "goal": detail.get("goal"),
                    "autoStartStop": detail.get("autoStartStop"),
                    "incompleteIssuesDestinationId": dest,
                    "incomplete_destination_kind": kind,
                    "board_url": sprint_svc.sprint_board_url(
                        jira_config.url,
                        int(detail.get("originBoardId") or 0),
                        int(detail["id"]),
                    )
                    if detail.get("originBoardId")
                    else None,
                }
                if args.format in ("json", "both"):
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                if args.format in ("text", "both"):
                    print(
                        f"Спринт: {payload['name']}\n"
                        f"autoStartStop: {payload['autoStartStop']}\n"
                        f"перенос незавершённых: {kind} "
                        f"(id={dest})\n"
                        f"goal:\n{payload.get('goal') or '—'}"
                    )
                return

            if args.preview_file:
                preview = _load_json(args.preview_file)
            else:
                if not args.start or not args.end:
                    print(
                        "Ошибка: нужны --start и --end (или --preview-file / --verify)",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                start = sm.parse_user_date(args.start)
                end = sm.parse_user_date(args.end)
                goals = _load_goals(args)
                preview = sm.build_preview(
                    jira,
                    conf,
                    start=start,
                    end=end,
                    goals=goals,
                    jira_config_url=jira_config.url,
                    project_key=args.project or jira_config.project,
                    board_id_override=jira_config.board_id or None,
                    mgmt_config=mgmt_cfg,
                )

            if not args.apply:
                if args.format in ("text", "both"):
                    print(sm.format_preview_text(preview))
                if args.format in ("json", "both"):
                    print(json.dumps(preview, ensure_ascii=False, indent=2))
                sys.exit(0 if preview.get("ready") else 2)

            resume = _load_json(args.resume) if args.resume else None
            result = sm.apply_create(
                jira, conf, preview, resume_state=resume
            )
            if args.format in ("text", "both"):
                print(sm.format_result_text(result))
            if args.format in ("json", "both"):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result.get("ok") else 3)

    except (JiraError, ConfluenceError, ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
