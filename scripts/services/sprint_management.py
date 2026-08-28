"""Create and configure CAT2 sprints + Confluence sprint pages.

Preview by default. Write only via apply_* after user confirmation.
Does not move issues or change previous sprint contents.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from confluence_client import ConfluenceClient
from jira_client import JiraClient
from services import sprints as sprint_svc

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SCRIPTS_DIR / "config" / "sprint_management.yaml"

# Destination id meaning "move incomplete issues to backlog".
BACKLOG_DESTINATION_ID = -1

_DATED_SPRINT_RE = re.compile(
    r"^(?:(?P<num>\d+)\.\s*)?CAT2\s+"
    r"(?P<d1>\d{1,2})\.(?P<m1>\d{1,2})(?:\.(?P<y1>\d{2,4}))?\s*[-–]\s*"
    r"(?P<d2>\d{1,2})\.(?P<m2>\d{1,2})(?:\.(?P<y2>\d{2,4}))?\s*$",
    re.IGNORECASE,
)
_PAGE_NUM_RE = re.compile(r"^(?P<num>\d+)\.\s*CAT2\b", re.IGNORECASE)


def load_sprint_management_config(
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Некорректный конфиг: {cfg_path}")
    return data


def parse_user_date(value: str) -> date:
    """Parse DD.MM.YY / DD.MM.YYYY / YYYY-MM-DD."""
    text = (value or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Не распознал дату {value!r}. Ожидаю DD.MM.YY или YYYY-MM-DD."
    )


def format_display_date(d: date) -> str:
    return d.strftime("%d.%m.%y")


def format_sprint_name(number: int, start: date, end: date) -> str:
    return (
        f"{number}. CAT2 {format_display_date(start)} - "
        f"{format_display_date(end)}"
    )


def _schedule_datetime(
    d: date,
    *,
    hour: int,
    minute: int,
    tz_offset: str,
) -> str:
    return (
        f"{d.isoformat()}T{hour:02d}:{minute:02d}:00.000{tz_offset}"
    )


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _is_dated_cat2_sprint(name: str) -> bool:
    return bool(_DATED_SPRINT_RE.match((name or "").strip()))


def _sprint_number_from_name(name: str) -> Optional[int]:
    m = _DATED_SPRINT_RE.match((name or "").strip())
    if not m:
        return None
    num = m.group("num")
    return int(num) if num else None


def classify_incomplete_destination(
    destination_id: Optional[int],
    *,
    backlog_id: int = BACKLOG_DESTINATION_ID,
    known_sprint_ids: Optional[Sequence[int]] = None,
) -> str:
    """Return human label: next_sprint | backlog | unset | unknown."""
    if destination_id is None:
        return "unset"
    if destination_id == backlog_id:
        return "backlog"
    if known_sprint_ids is not None and destination_id in known_sprint_ids:
        return "next_sprint"
    if destination_id > 0:
        return "next_sprint"
    return "unknown"


def list_child_pages(
    confluence: ConfluenceClient,
    parent_page_id: str,
    *,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    data = confluence.get(
        f"/rest/api/content/{parent_page_id}/child/page",
        params={"limit": limit, "expand": "version"},
    )
    return list(data.get("results") or [])


def find_page_by_title(
    pages: Sequence[Dict[str, Any]],
    title: str,
) -> Optional[Dict[str, Any]]:
    title_norm = title.strip().lower()
    for page in pages:
        if (page.get("title") or "").strip().lower() == title_norm:
            return page
    return None


def next_sprint_number_from_pages(
    pages: Sequence[Dict[str, Any]],
) -> int:
    max_num = 0
    for page in pages:
        m = _PAGE_NUM_RE.match((page.get("title") or "").strip())
        if m:
            max_num = max(max_num, int(m.group("num")))
    return max_num + 1


def build_confluence_storage_body(
    *,
    dashboard_url: str,
    goals: Sequence[str],
) -> str:
    """Storage format aligned with existing CAT2 sprint pages (toc + h1)."""
    goals_html = "<br />".join(
        _escape_html(f"{idx}. {goal}") for idx, goal in enumerate(goals, 1)
    )
    if not goals_html:
        goals_html = "<em>Цели будут добавлены.</em>"

    dash = _escape_html(dashboard_url)
    return (
        '<ac:layout>'
        '<ac:layout-section ac:type="single"><ac:layout-cell>'
        '<p><ac:structured-macro ac:name="toc" ac:schema-version="1" '
        '/></p>'
        "</ac:layout-cell></ac:layout-section>"
        '<ac:layout-section ac:type="single"><ac:layout-cell>'
        "<h1>Дашборд</h1>"
        f'<p><a href="{dash}">{dash}</a></p>'
        "<h1>Цели</h1>"
        f"<p>{goals_html}</p>"
        "<h1>Результаты</h1>"
        "<p><em>Заполняется при завершении спринта.</em></p>"
        "</ac:layout-cell></ac:layout-section>"
        "</ac:layout>"
    )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_jira_goal(
    goals: Sequence[str],
    confluence_url: str,
    *,
    max_chars: int = 8000,
) -> str:
    lines = [f"{idx}. {goal}" for idx, goal in enumerate(goals, 1)]
    footer = f"\n\nПодробнее: {confluence_url}"
    body = "\n".join(lines)
    full = body + footer
    if len(full) <= max_chars:
        return full

    # Keep Confluence link; shorten goals.
    budget = max_chars - len(footer) - 20
    shortened: List[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if shortened else 0)
        if used + extra > budget:
            break
        shortened.append(line)
        used += extra
    if not shortened and lines:
        shortened = [lines[0][: max(20, budget)]]
    return "\n".join(shortened) + footer


def page_web_url(confluence_base: str, page: Dict[str, Any]) -> str:
    links = page.get("_links") or {}
    webui = links.get("webui") or ""
    base = (links.get("base") or confluence_base).rstrip("/")
    if webui:
        return f"{base}{webui}"
    page_id = page.get("id")
    return f"{confluence_base.rstrip('/')}/pages/viewpage.action?pageId={page_id}"


def _collect_dated_sprints(
    client: JiraClient,
    board_id: int,
) -> List[Dict[str, Any]]:
    raw = sprint_svc.list_all_sprints(
        client, board_id, state="active,future,closed"
    )
    dated: List[Dict[str, Any]] = []
    for item in raw:
        name = item.get("name") or ""
        if not _is_dated_cat2_sprint(name):
            continue
        start = _parse_iso_date(item.get("startDate"))
        end = _parse_iso_date(item.get("endDate"))
        if not start or not end:
            continue
        dated.append({**item, "_start": start, "_end": end})
    dated.sort(key=lambda s: (s["_start"], s["_end"], s.get("id") or 0))
    return dated


def _find_previous_and_next(
    dated: Sequence[Dict[str, Any]],
    start: date,
    end: date,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    previous = None
    for sprint in dated:
        if sprint["_end"] < start:
            previous = sprint
        else:
            break
    following = None
    for sprint in dated:
        if sprint["_start"] > end:
            following = sprint
            break
    return previous, following


def _find_duplicate(
    dated: Sequence[Dict[str, Any]],
    start: date,
    end: date,
    name: str,
) -> Optional[Dict[str, Any]]:
    name_l = name.strip().lower()
    bare = re.sub(r"^\d+\.\s*", "", name, count=1).strip().lower()
    for sprint in dated:
        s_name = (sprint.get("name") or "").strip().lower()
        s_bare = re.sub(r"^\d+\.\s*", "", s_name, count=1)
        if sprint["_start"] == start and sprint["_end"] == end:
            return sprint
        if s_name == name_l or s_bare == bare:
            return sprint
    return None


def build_preview(
    jira: JiraClient,
    confluence: ConfluenceClient,
    *,
    start: date,
    end: date,
    goals: Sequence[str],
    jira_config_url: str,
    project_key: str,
    board_id_override: Optional[str] = None,
    mgmt_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a non-mutating preview of sprint + Confluence page creation."""
    if end < start:
        raise ValueError("Дата окончания раньше даты начала")

    cfg = mgmt_config or load_sprint_management_config()
    schedule = cfg.get("schedule") or {}
    conf_cfg = cfg.get("confluence") or {}
    parent_page_id = str(conf_cfg.get("parent_page_id") or "823380099")
    space_key = str(conf_cfg.get("space_key") or "BIZ")
    tz = str(schedule.get("timezone_offset") or "+05:00")
    start_hour = int(schedule.get("start_hour", 10))
    start_minute = int(schedule.get("start_minute", 0))
    end_hour = int(schedule.get("end_hour", 22))
    end_minute = int(schedule.get("end_minute", 0))
    auto_start_stop = bool(cfg.get("auto_start_stop", True))
    dest_cfg = cfg.get("incomplete_destination") or {}
    backlog_id = int(dest_cfg.get("backlog_id", BACKLOG_DESTINATION_ID))
    prefer_next = bool(dest_cfg.get("prefer_next_sprint", True))
    goal_max = int(cfg.get("goal_max_chars", 8000))

    board_id = sprint_svc.find_board_id(
        jira, project_key, board_id_override
    )
    dated = _collect_dated_sprints(jira, board_id)
    known_ids = [int(s["id"]) for s in dated]

    pages = list_child_pages(confluence, parent_page_id)
    number = next_sprint_number_from_pages(pages)
    name = format_sprint_name(number, start, end)

    duplicate = _find_duplicate(dated, start, end, name)
    page_dup = find_page_by_title(pages, name)
    # Also match unnumbered / same dates in Confluence titles.
    if not page_dup:
        want = f"CAT2 {format_display_date(start)} - {format_display_date(end)}"
        for page in pages:
            title = page.get("title") or ""
            if want.lower() in title.lower():
                page_dup = page
                break

    # Align number/name with existing page when dates already used.
    if page_dup:
        existing_num = _sprint_number_from_name(page_dup.get("title") or "")
        if existing_num is not None:
            number = existing_num
            name = format_sprint_name(number, start, end)
    elif duplicate:
        existing_num = _sprint_number_from_name(duplicate.get("name") or "")
        if existing_num is not None:
            number = existing_num
            name = format_sprint_name(number, start, end)

    previous, following = _find_previous_and_next(dated, start, end)

    previous_detail = None
    previous_dest_kind = "unset"
    if previous:
        previous_detail = sprint_svc.get_sprint_details(
            jira, int(previous["id"])
        )
        previous_dest_kind = classify_incomplete_destination(
            previous_detail.get("incompleteIssuesDestinationId"),
            backlog_id=backlog_id,
            known_sprint_ids=known_ids,
        )

    # Target for the NEW sprint's incomplete issues.
    new_dest_id: Optional[int]
    new_dest_label: str
    new_dest_manual = False
    if following and prefer_next:
        new_dest_id = int(following["id"])
        new_dest_label = (
            f"следующий спринт ({following.get('name')}, id={new_dest_id})"
        )
    else:
        new_dest_id = backlog_id
        new_dest_label = "backlog (следующий спринт ещё не создан)"
        new_dest_manual = True

    # What we will do to the previous sprint destination.
    prev_update: Optional[Dict[str, Any]] = None
    if previous and prefer_next:
        if previous_dest_kind in ("backlog", "unset"):
            prev_update = {
                "sprint_id": int(previous["id"]),
                "sprint_name": previous.get("name"),
                "from": previous_dest_kind,
                "to": "new_sprint",
                "action": (
                    "изменить incompleteIssuesDestinationId с backlog/unset "
                    "на id нового спринта"
                ),
            }
        elif previous_dest_kind == "next_sprint":
            prev_update = {
                "sprint_id": int(previous["id"]),
                "sprint_name": previous.get("name"),
                "from": previous_dest_kind,
                "to": "next_sprint",
                "action": "оставить перенос в следующий спринт (уже настроено)",
                "current_destination_id": previous_detail.get(
                    "incompleteIssuesDestinationId"
                )
                if previous_detail
                else None,
            }

    start_iso = _schedule_datetime(
        start, hour=start_hour, minute=start_minute, tz_offset=tz
    )
    end_iso = _schedule_datetime(
        end, hour=end_hour, minute=end_minute, tz_offset=tz
    )

    goals_list = [g.strip() for g in goals if (g or "").strip()]
    blocking: List[str] = []
    if duplicate:
        blocking.append(
            f"Спринт уже существует: {duplicate.get('name')} "
            f"(id={duplicate.get('id')})"
        )
    if page_dup:
        blocking.append(
            f"Страница Confluence уже есть: {page_dup.get('title')} "
            f"(id={page_dup.get('id')})"
        )
    if not goals_list:
        blocking.append(
            "Нет целей спринта — передай в --goal / --goals-file (или возьми из локального features/)"
        )

    settings_to_apply = [
        f"autoStartStop={auto_start_stop} (автозапуск и автозавершение по датам)",
        f"incompleteIssuesDestination нового спринта → {new_dest_label}",
    ]
    if prev_update and prev_update.get("to") == "new_sprint":
        settings_to_apply.append(prev_update["action"])
    if new_dest_manual:
        settings_to_apply.append(
            "после создания следующего спринта нужно будет обновить "
            "incompleteIssuesDestinationId этого спринта"
        )

    ready = not blocking
    return {
        "ready": ready,
        "blocking": blocking,
        "project": project_key,
        "board_id": board_id,
        "name": name,
        "number": number,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "start_date_display": format_display_date(start),
        "end_date_display": format_display_date(end),
        "start_datetime": start_iso,
        "end_datetime": end_iso,
        "goals": goals_list,
        "confluence": {
            "parent_page_id": parent_page_id,
            "parent_url": (
                f"{confluence.base_url}/pages/viewpage.action"
                f"?pageId={parent_page_id}"
            ),
            "space_key": space_key,
            "title": name,
        },
        "previous_sprint": (
            {
                "id": previous.get("id"),
                "name": previous.get("name"),
                "state": previous.get("state"),
                "start_date": previous.get("startDate"),
                "end_date": previous.get("endDate"),
                "incomplete_destination_kind": previous_dest_kind,
                "incomplete_destination_id": (
                    previous_detail.get("incompleteIssuesDestinationId")
                    if previous_detail
                    else None
                ),
            }
            if previous
            else None
        ),
        "following_sprint": (
            {
                "id": following.get("id"),
                "name": following.get("name"),
            }
            if following
            else None
        ),
        "carry_over": {
            "new_sprint_destination_id": new_dest_id,
            "new_sprint_destination_label": new_dest_label,
            "needs_next_sprint_later": new_dest_manual,
            "previous_sprint_update": prev_update,
            "mechanism": (
                "Agile field incompleteIssuesDestinationId "
                "(не Automation, задачи не переносятся сейчас)"
            ),
        },
        "auto_start_stop": {
            "enabled": auto_start_stop,
            "mechanism": (
                "Agile field autoStartStop=true "
                "(нативный автозапуск/автозавершение по startDate/endDate)"
            ),
            "requires_manual": not auto_start_stop,
        },
        "settings_to_apply": settings_to_apply,
        "goal_max_chars": goal_max,
        "jira_base_url": jira_config_url.rstrip("/"),
        "duplicate_sprint": (
            {
                "id": duplicate.get("id"),
                "name": duplicate.get("name"),
            }
            if duplicate
            else None
        ),
        "duplicate_page": (
            {
                "id": page_dup.get("id"),
                "title": page_dup.get("title"),
            }
            if page_dup
            else None
        ),
    }


def apply_create(
    jira: JiraClient,
    confluence: ConfluenceClient,
    preview: Dict[str, Any],
    *,
    resume_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create sprint + Confluence page + configure fields.

    Idempotent resume: pass resume_state with already created ids.
    """
    if not preview.get("ready"):
        raise ValueError(
            "Preview не ready: " + "; ".join(preview.get("blocking") or [])
        )

    state: Dict[str, Any] = {
        "steps": {
            "sprint_created": False,
            "confluence_created": False,
            "goal_written": False,
            "auto_start_stop_set": False,
            "new_carry_over_set": False,
            "previous_carry_over_updated": False,
            "verified": False,
        },
        "sprint": None,
        "confluence_page": None,
        "errors": [],
        "manual_required": [],
    }
    if resume_state:
        state.update({k: v for k, v in resume_state.items() if k != "steps"})
        state["steps"] = {
            **state["steps"],
            **(resume_state.get("steps") or {}),
        }

    board_id = int(preview["board_id"])
    name = preview["name"]
    goals = list(preview.get("goals") or [])
    goal_max = int(preview.get("goal_max_chars") or 8000)
    jira_base = preview["jira_base_url"]
    conf = preview["confluence"]
    auto = preview.get("auto_start_stop") or {}
    carry = preview.get("carry_over") or {}

    try:
        # 1. Create sprint
        if not state["steps"]["sprint_created"]:
            dest_on_create = carry.get("new_sprint_destination_id")
            created = sprint_svc.create_sprint(
                jira,
                name=name,
                board_id=board_id,
                start_date=preview["start_datetime"],
                end_date=preview["end_datetime"],
                goal="",
                auto_start_stop=bool(auto.get("enabled", True)),
                incomplete_issues_destination_id=(
                    int(dest_on_create) if dest_on_create is not None else None
                ),
            )
            state["sprint"] = created
            state["steps"]["sprint_created"] = True
            state["steps"]["auto_start_stop_set"] = bool(
                created.get("autoStartStop")
            )
            if dest_on_create is not None and (
                created.get("incompleteIssuesDestinationId") == dest_on_create
            ):
                state["steps"]["new_carry_over_set"] = True
        sprint = state["sprint"]
        sprint_id = int(sprint["id"])
        dashboard_url = sprint_svc.sprint_board_url(
            jira_base, board_id, sprint_id
        )
        state["dashboard_url"] = dashboard_url

        # 2. Create Confluence page
        if not state["steps"]["confluence_created"]:
            body = build_confluence_storage_body(
                dashboard_url=dashboard_url,
                goals=goals,
            )
            page = confluence.post(
                "/rest/api/content",
                json_body={
                    "type": "page",
                    "title": conf["title"],
                    "space": {"key": conf["space_key"]},
                    "ancestors": [{"id": conf["parent_page_id"]}],
                    "body": {
                        "storage": {
                            "value": body,
                            "representation": "storage",
                        }
                    },
                },
            )
            state["confluence_page"] = {
                "id": page.get("id"),
                "title": page.get("title"),
                "url": page_web_url(confluence.base_url, page),
            }
            state["steps"]["confluence_created"] = True

        page_info = state["confluence_page"]
        page_url = page_info["url"]

        # 3. Write goal + ensure autoStartStop
        if not state["steps"]["goal_written"]:
            goal_text = build_jira_goal(
                goals, page_url, max_chars=goal_max
            )
            updated = sprint_svc.update_sprint(
                jira,
                sprint_id,
                goal=goal_text,
                auto_start_stop=bool(auto.get("enabled", True)),
            )
            state["sprint"] = updated
            state["steps"]["goal_written"] = True
            state["steps"]["auto_start_stop_set"] = bool(
                updated.get("autoStartStop")
            )

        # 4. Carry-over for NEW sprint
        if not state["steps"]["new_carry_over_set"]:
            dest_id = carry.get("new_sprint_destination_id")
            if dest_id is None:
                state["manual_required"].append(
                    "Не удалось определить destination для незавершённых задач"
                )
            else:
                updated = sprint_svc.update_sprint(
                    jira,
                    sprint_id,
                    incomplete_issues_destination_id=int(dest_id),
                )
                state["sprint"] = updated
                state["steps"]["new_carry_over_set"] = True
                if carry.get("needs_next_sprint_later"):
                    state["manual_required"].append(
                        "Следующий спринт ещё не создан: "
                        "incompleteIssuesDestination сейчас backlog (−1). "
                        "После создания следующего спринта обнови "
                        "destination этого спринта на его id."
                    )

        # 5. Update previous sprint destination if it pointed to backlog
        prev_upd = carry.get("previous_sprint_update") or {}
        if (
            prev_upd
            and prev_upd.get("to") == "new_sprint"
            and not state["steps"]["previous_carry_over_updated"]
        ):
            updated_prev = sprint_svc.update_sprint(
                jira,
                int(prev_upd["sprint_id"]),
                incomplete_issues_destination_id=sprint_id,
            )
            state["previous_sprint_after"] = {
                "id": updated_prev.get("id"),
                "name": updated_prev.get("name"),
                "incompleteIssuesDestinationId": updated_prev.get(
                    "incompleteIssuesDestinationId"
                ),
            }
            state["steps"]["previous_carry_over_updated"] = True
        elif prev_upd and prev_upd.get("to") == "next_sprint":
            state["steps"]["previous_carry_over_updated"] = True

        # 6. Verify
        verified = sprint_svc.get_sprint_details(jira, sprint_id)
        state["sprint"] = verified
        page_check = confluence.get(
            f"/rest/api/content/{page_info['id']}",
            params={"expand": "version,space"},
        )
        state["confluence_page"] = {
            "id": page_check.get("id"),
            "title": page_check.get("title"),
            "url": page_web_url(confluence.base_url, page_check),
        }
        state["verification"] = {
            "sprint_name": verified.get("name"),
            "autoStartStop": verified.get("autoStartStop"),
            "incompleteIssuesDestinationId": verified.get(
                "incompleteIssuesDestinationId"
            ),
            "goal": verified.get("goal"),
            "confluence_title": page_check.get("title"),
        }
        state["steps"]["verified"] = True
        state["ok"] = True
    except Exception as exc:  # noqa: BLE001 — surface partial progress
        state["ok"] = False
        state["errors"].append(str(exc))
        state["resume_hint"] = (
            "Повтори apply с --resume <этот JSON>, "
            "успешные шаги не будут выполнены повторно."
        )
    return state


def format_preview_text(preview: Dict[str, Any]) -> str:
    goals = preview.get("goals") or []
    goals_block = (
        "\n".join(f"  {i}. {g}" for i, g in enumerate(goals, 1))
        or "  (нет)"
    )
    prev = preview.get("previous_sprint")
    prev_line = (
        f"{prev.get('name')} (id={prev.get('id')}, "
        f"перенос сейчас: {prev.get('incomplete_destination_kind')})"
        if prev
        else "—"
    )
    conf = preview.get("confluence") or {}
    settings = "\n".join(
        f"  - {s}" for s in (preview.get("settings_to_apply") or [])
    )
    blocking = preview.get("blocking") or []
    block_txt = (
        "\n".join(f"  - {b}" for b in blocking) if blocking else "  (нет)"
    )
    carry = preview.get("carry_over") or {}
    auto = preview.get("auto_start_stop") or {}

    return (
        f"Preview создания спринта (ready: "
        f"{'yes' if preview.get('ready') else 'no'})\n\n"
        f"Название: {preview.get('name')}\n"
        f"Даты: {preview.get('start_date_display')} — "
        f"{preview.get('end_date_display')}\n"
        f"Доска: {preview.get('project')} / board_id="
        f"{preview.get('board_id')}\n"
        f"Предыдущий спринт: {prev_line}\n"
        f"Родитель Confluence: {conf.get('parent_url')} "
        f"(pageId={conf.get('parent_page_id')})\n"
        f"Страница: {conf.get('title')}\n"
        f"Проект целей: из --goal / --goals-file (опционально локальный features/)\n\n"
        f"Цели:\n{goals_block}\n\n"
        f"Правило переноса незавершённых:\n"
        f"  новый спринт → {carry.get('new_sprint_destination_label')}\n"
        f"  механизм: {carry.get('mechanism')}\n\n"
        f"Автозапуск/завершение:\n"
        f"  {auto.get('mechanism')}\n\n"
        f"Будет применено:\n{settings}\n\n"
        f"Блокеры:\n{block_txt}\n\n"
        "Подтверди цели (изменить / добавить / удалить) и создание.\n"
        "Без подтверждения ничего не создаётся."
    )


def format_result_text(result: Dict[str, Any]) -> str:
    sprint = result.get("sprint") or {}
    page = result.get("confluence_page") or {}
    ver = result.get("verification") or {}
    dest = ver.get("incompleteIssuesDestinationId")
    if dest == BACKLOG_DESTINATION_ID:
        dest_label = "backlog (−1) — нужен следующий спринт"
    elif dest:
        dest_label = f"в спринт id={dest}"
    else:
        dest_label = "не задано"

    auto_ok = bool(ver.get("autoStartStop"))
    goals_raw = (ver.get("goal") or "").strip()
    goal_lines = [
        ln for ln in goals_raw.splitlines()
        if ln.strip() and not ln.strip().lower().startswith("подробнее:")
    ]

    manual = result.get("manual_required") or []
    errors = result.get("errors") or []
    status = "Спринт создан" if result.get("ok") else "Частичный результат"

    lines = [
        f"{status}: {sprint.get('name') or '—'}",
        f"Jira: {result.get('dashboard_url') or sprint.get('self') or '—'}",
        f"Confluence: {page.get('url') or '—'}",
        "",
        "Настройки:",
        f"- автоматический запуск: "
        f"{'настроен' if auto_ok else 'не подтверждён'};",
        f"- автоматическое завершение: "
        f"{'настроено' if auto_ok else 'не подтверждено'};",
        f"- незавершённые задачи: {dest_label}.",
        "",
        "Цели:",
    ]
    if goal_lines:
        lines.extend(goal_lines)
    else:
        lines.append("(см. Confluence)")

    if manual:
        lines.append("")
        lines.append("Требует внимания:")
        lines.extend(f"- {m}" for m in manual)
    if errors:
        lines.append("")
        lines.append("Ошибки:")
        lines.extend(f"- {e}" for e in errors)
        if result.get("resume_hint"):
            lines.append(result["resume_hint"])

    # Steps summary for partial failure
    steps = result.get("steps") or {}
    if not result.get("ok"):
        lines.append("")
        lines.append("Шаги:")
        for key, done in steps.items():
            lines.append(f"- {key}: {'ok' if done else 'pending'}")

    return "\n".join(lines)
