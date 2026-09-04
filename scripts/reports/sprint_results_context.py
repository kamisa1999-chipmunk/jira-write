"""Collect verified sprint-results context. Does not write the Mattermost text."""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from confluence_client import (
    ConfluenceClient,
    ConfluenceConfigError,
    ConfluenceError,
    load_confluence_config,
)
from jira_client import JiraClient, JiraConfig
from models.issue import normalize_issue
from models.sprint import normalize_sprint, sprint_date_fragment
from services import issues as issues_service
from services import sprints as sprints_service

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
NOTES_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SPRINTS_DIR = NOTES_ROOT / "sprints"
CACHE_MAX_AGE = timedelta(hours=8)

RESULTS_ISSUE_FIELDS = (
    "summary,description,status,assignee,reporter,issuetype,priority,"
    "created,updated,resolutiondate,labels,comment,issuelinks,parent,"
    "components,customfield_12201,customfield_10014"
)

CONFLUENCE_URL_RE = re.compile(
    r"https?://[^\s)>\]]*confluence[^\s)>\]]*",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
HEADING_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
WS_RE = re.compile(r"\n{3,}")
RELEASE_HINT_RE = re.compile(
    r"(прод|раскат|выкат|включ\w*\s*ft|feature.?toggle|rolled.?out|"
    r"release|to prod|зарелизил)",
    re.IGNORECASE,
)
NOTE_GLOBS = ("*.md", "*.txt", "*.json")
MAX_NOTE_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 800
MAX_COMMENT_CHARS = 400


def parse_jira_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    if len(cleaned) >= 5 and cleaned[-5] in "+-" and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        return None


def _parse_generated_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def find_fresh_snapshot(
    sprint_id: int,
    *,
    project: str,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    max_age: timedelta = CACHE_MAX_AGE,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Newest sprint snapshot JSON for this sprint younger than max_age."""
    now = now or datetime.now().astimezone()
    marker = f"__{project}__sprint-{sprint_id}__"
    newest: Optional[Tuple[datetime, Path]] = None
    if not reports_dir.is_dir():
        return None
    for path in reports_dir.glob("*.json"):
        name = path.name
        if marker not in name or "sprint-results-" in name:
            continue
        if "testing-monitor" in name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int((payload.get("sprint") or {}).get("id") or 0) != int(sprint_id):
            continue
        generated = _parse_generated_at(payload.get("report_generated_at"))
        if generated is None:
            continue
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=now.tzinfo or timezone.utc)
        if now - generated > max_age:
            continue
        if newest is None or generated > newest[0]:
            newest = (generated, path)
    return newest[1] if newest else None


def find_fresh_results_context(
    sprint_id: int,
    *,
    project: str,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    max_age: timedelta = CACHE_MAX_AGE,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    now = now or datetime.now().astimezone()
    marker = f"__{project}__sprint-results-{sprint_id}__"
    newest: Optional[Tuple[datetime, Path]] = None
    if not reports_dir.is_dir():
        return None
    for path in reports_dir.glob("*.json"):
        if marker not in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        generated = _parse_generated_at(payload.get("report_generated_at"))
        if generated is None:
            continue
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=now.tzinfo or timezone.utc)
        if now - generated > max_age:
            continue
        if newest is None or generated > newest[0]:
            newest = (generated, path)
    return newest[1] if newest else None


def _sprint_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    end = parse_jira_date(raw.get("endDate"))
    start = parse_jira_date(raw.get("startDate"))
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "state": raw.get("state"),
        "startDate": raw.get("startDate"),
        "endDate": raw.get("endDate"),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "goal": raw.get("goal") or "",
    }


def select_sprint_for_results(
    sprints: List[Dict[str, Any]],
    *,
    today: Optional[date] = None,
    sprint_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Pick a finished or finishing sprint. Never silently prefer a later active one."""
    today = today or date.today()
    indexed = {int(item["id"]): item for item in sprints if item.get("id") is not None}

    if sprint_id is not None:
        found = indexed.get(int(sprint_id))
        if found is None:
            return {
                "status": "missing",
                "reason": f"Спринт {sprint_id} не найден на доске",
                "sprint_id": int(sprint_id),
                "candidates": [_sprint_candidate(item) for item in sprints],
            }
        return {
            "status": "chosen",
            "reason": "explicit id",
            "sprint": found,
            "candidates": [],
        }

    dated: List[Tuple[Dict[str, Any], date]] = []
    for item in sprints:
        end = parse_jira_date(item.get("endDate"))
        if end is None:
            continue
        dated.append((item, end))
    if not dated:
        return {
            "status": "ambiguous",
            "reason": "На доске нет спринтов с датой окончания",
            "candidates": [_sprint_candidate(item) for item in sprints[:15]],
        }

    def dist(end: date) -> int:
        return abs((end - today).days)

    min_dist = min(dist(end) for _, end in dated)
    closest = [(item, end) for item, end in dated if dist(end) == min_dist]
    preferred = [
        (item, end)
        for item, end in closest
        if str(item.get("state") or "").lower() == "closed" or end == today
    ]
    just_closed = [
        (item, end)
        for item, end in dated
        if str(item.get("state") or "").lower() == "closed"
        and 0 <= (today - end).days <= 3
    ]
    active_not_ending = [
        (item, end)
        for item, end in closest
        if str(item.get("state") or "").lower() == "active" and end != today
    ]

    if active_not_ending and just_closed:
        pool = just_closed + active_not_ending
        return {
            "status": "ambiguous",
            "reason": (
                "Рядом есть только что закрытый спринт и активный. "
                "Не выбираю активный молча."
            ),
            "candidates": [_sprint_candidate(item) for item, _ in pool],
        }

    pool = preferred or closest
    if len(pool) != 1:
        return {
            "status": "ambiguous",
            "reason": "Несколько спринтов одинаково близки к сегодняшней дате",
            "candidates": [_sprint_candidate(item) for item, _ in pool],
        }

    chosen, end = pool[0]
    reason = "closest end date"
    if end == today:
        reason = "ends today"
    elif str(chosen.get("state") or "").lower() == "closed":
        reason = "closed, closest end date"
    return {
        "status": "chosen",
        "reason": reason,
        "sprint": chosen,
        "candidates": [],
    }


def extract_confluence_refs(goal: str) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    seen = set()
    for match in CONFLUENCE_URL_RE.finditer(goal or ""):
        url = match.group(0).rstrip(".,;\"'")
        if url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        page_id = (query.get("pageId") or [""])[0]
        space = ""
        title = ""
        parts = [part for part in parsed.path.split("/") if part]
        if not page_id and len(parts) >= 2 and parts[0] == "pages" and parts[1].isdigit():
            page_id = parts[1]
        if not page_id and len(parts) >= 3 and parts[0] == "display":
            space = unquote(parts[1])
            title = unquote(parts[2]).replace("+", " ")
        refs.append(
            {
                "url": url,
                "page_id": page_id,
                "space_key": space,
                "title": title,
            }
        )
    return refs


def storage_to_markdown(storage: str) -> str:
    def heading_sub(match: re.Match[str]) -> str:
        level = int(match.group(1))
        inner = TAG_RE.sub(" ", match.group(2))
        inner = html.unescape(re.sub(r"[ \t]+", " ", inner)).strip()
        return f"\n{'#' * level} {inner}\n"

    text = HEADING_RE.sub(heading_sub, storage or "")
    text = (
        text.replace("<br />", "\n")
        .replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("</p>", "\n")
        .replace("</tr>", "\n")
        .replace("</li>", "\n")
    )
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return WS_RE.sub("\n\n", "\n".join(lines)).strip()


def split_named_sections(markdown: str) -> Dict[str, str]:
    """Keep Дашборд / Цели / Результаты (with nested headings)."""
    wanted = {
        "дашборд": "dashboard",
        "цели": "goals",
        "результаты": "results",
    }
    blocks: Dict[str, List[str]] = {key: [] for key in wanted.values()}
    current: Optional[str] = None
    for line in (markdown or "").splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(2).strip().rstrip(":").lower()
            mapped = wanted.get(title)
            if mapped:
                current = mapped
                continue
            if current == "results":
                blocks[current].append(line)
                continue
            if mapped is None and title in wanted:
                current = wanted[title]
                continue
        if current:
            blocks[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in blocks.items() if "".join(lines).strip()}


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "…"


def compact_issue(normalized: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    fields = raw.get("fields") or {}
    parent = fields.get("parent") or {}
    epic_raw = fields.get("customfield_10014")
    epic_key = None
    if isinstance(epic_raw, str):
        epic_key = epic_raw
    elif isinstance(epic_raw, dict):
        epic_key = epic_raw.get("key") or epic_raw.get("name")

    comments = []
    for comment in (normalized.get("comments") or [])[-3:]:
        body = _truncate(comment.get("body"), MAX_COMMENT_CHARS)
        if not body:
            continue
        comments.append(
            {
                "author": (comment.get("author") or {}).get("display_name"),
                "created": comment.get("created"),
                "body": body,
            }
        )

    release_hints = []
    for source, text in (
        ("description", normalized.get("description") or ""),
        *[("comment", item["body"]) for item in comments],
    ):
        if RELEASE_HINT_RE.search(text or ""):
            release_hints.append({"source": source, "excerpt": _truncate(text, 240)})

    return {
        "key": normalized.get("key"),
        "summary": normalized.get("summary"),
        "type": normalized.get("type"),
        "status": normalized.get("status"),
        "status_category": normalized.get("status_category"),
        "platform": normalized.get("platform"),
        "components": normalized.get("components") or [],
        "parent_key": parent.get("key"),
        "epic_key": epic_key,
        "is_testing_task": normalized.get("is_testing_task"),
        "url": normalized.get("url"),
        "links": [
            {
                "key": link.get("key"),
                "type": link.get("type"),
                "direction": link.get("direction"),
                "status": link.get("status"),
            }
            for link in (normalized.get("links") or [])[:8]
        ],
        "description": _truncate(normalized.get("description"), MAX_DESCRIPTION_CHARS),
        "recent_comments": comments,
        "release_hints": release_hints,
        "stage_hint": _stage_hint(normalized.get("status"), normalized.get("status_category")),
    }


def _stage_hint(status: Optional[str], category: Optional[str]) -> str:
    name = status or ""
    if name == "To Prod" or category == "ready_to_prod":
        return "ready_for_release_not_prod"
    if name == "Done" or category == "closed":
        return "jira_done_not_automatically_prod"
    if name in {"Testing", "To Test"} or category in {"testing_queue", "in_progress"}:
        if name in {"Testing", "To Test"}:
            return "testing_not_shipped"
    if name in {"Discovery", "To Discovery", "Review"} or category in {
        "discovery",
        "returned_or_discovery",
    }:
        return "analysis_or_design"
    if name in {"Canceled", "Postponed"}:
        return "stopped"
    return "in_progress_or_other"


def fetch_confluence_page(ref: Dict[str, str]) -> Dict[str, Any]:
    config = load_confluence_config()
    with ConfluenceClient(config) as client:
        page = None
        if ref.get("page_id"):
            page = client.get(
                f"/rest/api/content/{ref['page_id']}",
                params={"expand": "body.storage,version,space"},
            )
        elif ref.get("space_key") and ref.get("title"):
            titles = [ref["title"], ref["title"].replace("-", "–")]
            for title in titles:
                data = client.get(
                    "/rest/api/content",
                    params={
                        "spaceKey": ref["space_key"],
                        "title": title,
                        "expand": "body.storage,version,space",
                    },
                )
                results = data.get("results") or []
                if results:
                    page = results[0]
                    break
        if page is None:
            raise ConfluenceError("Страница Confluence не найдена по ссылке из goal")

        page_id = str(page.get("id") or "")
        storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        markdown = storage_to_markdown(storage)
        sections = split_named_sections(markdown)
        web_url = (
            f"{config.url}/pages/viewpage.action?pageId={page_id}"
            if page_id
            else ref.get("url")
        )
        return {
            "ok": True,
            "page_id": page_id,
            "title": page.get("title"),
            "url": web_url,
            "version": (page.get("version") or {}).get("number"),
            "sections": sections,
            "source_ref": ref,
        }


def find_local_sprint_notes(
    sprint: Dict[str, Any],
    *,
    sprints_dir: Path = DEFAULT_SPRINTS_DIR,
) -> Dict[str, Any]:
    if not sprints_dir.is_dir():
        return {
            "ok": False,
            "reason": f"Нет директории {sprints_dir}",
            "directory": None,
            "files": [],
        }

    sprint_id = str(sprint.get("id") or "")
    name = (sprint.get("name") or "").lower()
    start = parse_jira_date(sprint.get("startDate"))
    end = parse_jira_date(sprint.get("endDate"))
    tokens = []
    if sprint_id:
        tokens.append(sprint_id)
    if start:
        tokens.append(start.strftime("%d-%m"))
        tokens.append(start.strftime("%d.%m"))
        tokens.append(start.isoformat())
    if end:
        tokens.append(end.strftime("%d-%m"))
        tokens.append(end.strftime("%d.%m"))
    name_bits = re.findall(r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?", name)

    scored: List[Tuple[int, Path]] = []
    for path in sprints_dir.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        blob = path.name.lower()
        score = 0
        if sprint_id and sprint_id in blob:
            score += 5
        for token in tokens:
            if token and token.lower() in blob:
                score += 2
        for bit in name_bits:
            if bit and bit.replace(".", "-") in blob.replace(".", "-"):
                score += 1
        try:
            extra = "\n".join(
                child.read_text(encoding="utf-8", errors="ignore")[:2000]
                for child in path.glob("*.md")
            )
        except OSError:
            extra = ""
        if sprint_id and sprint_id in extra:
            score += 4
        if name and name[:20] in extra.lower():
            score += 2
        if score:
            scored.append((score, path))

    if not scored:
        return {
            "ok": False,
            "reason": "Локальная папка спринта не найдена",
            "directory": None,
            "files": [],
        }

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    if len(scored) > 1 and scored[1][0] == best_score:
        return {
            "ok": False,
            "reason": "Несколько локальных папок одинаково похожи на этот спринт",
            "directory": None,
            "candidates": [str(path) for _, path in scored[:5]],
            "files": [],
        }

    files = []
    for pattern in NOTE_GLOBS:
        for child in sorted(best.glob(pattern)):
            try:
                text = child.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files.append(
                {
                    "path": str(child),
                    "name": child.name,
                    "text": _truncate(text, MAX_NOTE_CHARS),
                }
            )
    return {
        "ok": True,
        "directory": str(best),
        "files": files,
    }


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def save_results_context(
    report: Dict[str, Any],
    *,
    reports_dir: Optional[Path] = None,
) -> Path:
    directory = reports_dir or DEFAULT_REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    sprint = report.get("sprint") or {}
    ts = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M")
    start = sprint_date_fragment(sprint.get("startDate"))
    end = sprint_date_fragment(sprint.get("endDate"))
    sprint_id = sprint.get("id") or "unknown"
    stem = (
        f"{ts}__{report['project']}__sprint-results-{sprint_id}__{start}__{end}"
    )
    path = _unique_path(directory / f"{stem}.json")
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _issue_index(issues: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("key")): item for item in issues if item.get("key")}


def build_composition_candidates(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Noteworthy clusters from the full sprint board, not only Confluence goals."""
    by_key = _issue_index(issues)
    skip_status = {"Canceled", "Postponed"}
    root_types = {
        "DeliveryStory",
        "Epic",
        "Analysis",
        "Research",
        "Specification",
    }
    progress_status = {
        "Done",
        "To Prod",
        "Testing",
        "To Test",
        "Review",
        "Development",
        "Code Review",
        "Discovery",
        "В работе",
    }

    def related_keys(root_key: str) -> List[str]:
        keys = {root_key}
        root = by_key.get(root_key) or {}
        for link in root.get("links") or []:
            key = link.get("key")
            if key in by_key:
                keys.add(key)
        for item in issues:
            if item.get("parent_key") == root_key or item.get("epic_key") == root_key:
                keys.add(item["key"])
        return sorted(keys)

    def members(keys: List[str]) -> List[Dict[str, Any]]:
        rows = []
        for key in keys:
            item = by_key.get(key)
            if not item or item.get("status") in skip_status:
                continue
            rows.append(
                {
                    "key": key,
                    "summary": item.get("summary"),
                    "status": item.get("status"),
                    "type": item.get("type"),
                    "platform": item.get("platform"),
                    "is_testing_task": bool(item.get("is_testing_task")),
                    "stage_hint": item.get("stage_hint"),
                }
            )
        return rows

    groups: List[Dict[str, Any]] = []
    used_roots = set()
    for item in issues:
        if item.get("type") not in root_types:
            continue
        if item.get("status") in skip_status:
            continue
        key = item["key"]
        rel = related_keys(key)
        rows = members(rel)
        progressing = [
            row
            for row in rows
            if row["status"] in progress_status and not row["is_testing_task"]
        ]
        testing_done = [
            row
            for row in rows
            if row["is_testing_task"] and row["status"] == "Done"
        ]
        if not progressing and not testing_done:
            continue
        if key in used_roots:
            continue
        used_roots.add(key)
        if item.get("type") == "Analysis":
            role = "analysis"
        elif "tech" in (item.get("summary") or "").lower() or "техдолг" in (
            item.get("summary") or ""
        ).lower():
            role = "tech"
        elif item.get("status") in {"Done", "To Prod"}:
            role = "shipped_or_ready"
        else:
            role = "in_progress"
        groups.append(
            {
                "id": key,
                "title": item.get("summary"),
                "suggested_role": role,
                "why_consider": (
                    "Корневая задача спринта с продвижением по связанным работам"
                ),
                "root_keys": [key],
                "member_keys": [row["key"] for row in rows],
                "members": rows[:30],
            }
        )

    standalone_tech = []
    grouped_keys = {key for group in groups for key in group["member_keys"]}
    for item in issues:
        if item["key"] in grouped_keys:
            continue
        if item.get("is_testing_task") or item.get("status") in skip_status:
            continue
        if item.get("type") not in {
            "DevelopmentB",
            "DevelopmentF",
            "sDevelopmentF",
            "sDevelopmentB",
        }:
            continue
        if item.get("status") not in {"Done", "To Prod"}:
            continue
        standalone_tech.append(
            {
                "key": item["key"],
                "summary": item.get("summary"),
                "status": item.get("status"),
                "type": item.get("type"),
                "platform": item.get("platform"),
            }
        )

    elogo = [
        item["key"]
        for item in issues
        if re.search(r"e-?logo|е-?лого", item.get("summary") or "", re.I)
        and item.get("status") not in skip_status
    ]

    return {
        "instruction": (
            "Цели Confluence задают приоритет, но не исчерпывают отчёт. "
            "Обязательно разбери groups, standalone_tech_done_or_to_prod и "
            "elogo_keys. Запрещено публиковать только цели страницы, если "
            "здесь есть значимая текущая или техническая работа."
        ),
        "groups": groups[:20],
        "standalone_tech_done_or_to_prod": standalone_tech[:25],
        "elogo_keys": elogo,
        "present_keys": {
            "CAT2-3641": "CAT2-3641" in by_key,
            "CAT2-3642": "CAT2-3642" in by_key,
            "CAT2-3976": "CAT2-3976" in by_key,
            "CAT2-2962": "CAT2-2962" in by_key,
            "CAT2-3610": "CAT2-3610" in by_key,
        },
    }


def build_sprint_results_context(
    client: JiraClient,
    config: JiraConfig,
    *,
    sprint_id: Optional[int] = None,
    refresh: bool = False,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    sprints_dir: Path = DEFAULT_SPRINTS_DIR,
    progress_stream=sys.stderr,
) -> Dict[str, Any]:
    log = progress_stream
    missing_sources: List[str] = []
    source_notes: List[str] = []

    print("Ищу доску и спринты CAT2...", file=log)
    board_id = sprints_service.find_board_id(
        client, config.project, config.board_id or None
    )
    board_sprints = sprints_service.list_all_sprints(
        client, board_id, state="active,future,closed"
    )
    selection = select_sprint_for_results(board_sprints, sprint_id=sprint_id)

    if selection["status"] != "chosen":
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "report_generated_at": generated_at,
            "project": config.project,
            "board_id": board_id,
            "selection": selection,
            "missing_sources": missing_sources,
            "source_notes": source_notes,
            "issues": [],
        }

    chosen_id = int(selection["sprint"]["id"])
    if not refresh:
        cached_path = find_fresh_results_context(
            chosen_id, project=config.project, reports_dir=reports_dir
        )
        if cached_path is not None:
            print(f"Беру свежий context-кэш: {cached_path.name}", file=log)
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            cached["reused_from"] = str(cached_path)
            return cached

    sprint_raw = sprints_service.get_sprint_details(client, chosen_id)
    sprint = normalize_sprint(sprint_raw)
    snapshot_path = None
    if not refresh:
        snapshot_path = find_fresh_snapshot(
            chosen_id, project=config.project, reports_dir=reports_dir
        )
        if snapshot_path is not None:
            source_notes.append(f"Переиспользован снимок Jira младше 8 ч: {snapshot_path.name}")

    print(f"Спринт: {sprint.get('name')} (id={sprint.get('id')})", file=log)
    print("Загружаю задачи спринта с описаниями и связями...", file=log)
    raw_issues = issues_service.get_sprint_issues(
        client, chosen_id, fields=RESULTS_ISSUE_FIELDS
    )
    project_prefix = f"{config.project}-"
    project_issues = [
        issue for issue in raw_issues if str(issue.get("key", "")).startswith(project_prefix)
    ]
    print(f"Найдено задач проекта: {len(project_issues)}", file=log)

    compact_issues = [
        compact_issue(normalize_issue(issue, client.base_url), issue)
        for issue in project_issues
    ]
    by_status: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    testing_count = 0
    with_release_hints = []
    for issue in compact_issues:
        status = issue.get("status") or "Без статуса"
        by_status[status] = by_status.get(status, 0) + 1
        hint = issue.get("stage_hint") or "other"
        by_stage[hint] = by_stage.get(hint, 0) + 1
        if issue.get("is_testing_task"):
            testing_count += 1
        if issue.get("release_hints"):
            with_release_hints.append(issue["key"])

    confluence: Dict[str, Any] = {
        "ok": False,
        "reason": "В goal нет ссылки на Confluence",
        "sections": {},
    }
    refs = extract_confluence_refs(sprint.get("goal") or "")
    if not refs:
        missing_sources.append("confluence")
    else:
        try:
            print("Читаю страницу Confluence из goal...", file=log)
            confluence = fetch_confluence_page(refs[0])
        except (ConfluenceConfigError, ConfluenceError, OSError) as exc:
            confluence = {
                "ok": False,
                "reason": str(exc),
                "source_ref": refs[0],
                "sections": {},
            }
            missing_sources.append("confluence")
            source_notes.append(f"Confluence недоступен: {exc}")

    print("Ищу локальные заметки в sprints/...", file=log)
    local_notes = find_local_sprint_notes(sprint_raw, sprints_dir=sprints_dir)
    if not local_notes.get("ok"):
        missing_sources.append("local_notes")
        source_notes.append(local_notes.get("reason") or "Нет локальных заметок")

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "report_generated_at": generated_at,
        "project": config.project,
        "board_id": board_id,
        "selection": {
            "status": "chosen",
            "reason": selection.get("reason"),
            "candidates": [],
        },
        "sprint": sprint,
        "jira_snapshot_reused": str(snapshot_path) if snapshot_path else None,
        "confluence": confluence,
        "local_notes": local_notes,
        "summary": {
            "total_issues": len(compact_issues),
            "testing_tasks": testing_count,
            "by_status": by_status,
            "by_stage_hint": by_stage,
            "keys_with_release_hints": with_release_hints,
        },
        "issues": compact_issues,
        "composition_candidates": build_composition_candidates(compact_issues),
        "writing_constraints": {
            "done_is_not_prod": True,
            "to_prod_is_not_release": True,
            "testing_is_not_shipped": True,
            "do_not_invent_keys_or_causes": True,
            "do_not_publish": True,
        },
        "missing_sources": missing_sources,
        "source_notes": source_notes,
    }


def print_text_summary(report: Dict[str, Any]) -> None:
    selection = report.get("selection") or {}
    if selection.get("status") != "chosen":
        print(f"Выбор спринта: {selection.get('status')} — {selection.get('reason')}")
        for item in selection.get("candidates") or []:
            print(
                f"  - id={item.get('id')} {item.get('name')} "
                f"state={item.get('state')} end={item.get('end')}"
            )
        return

    sprint = report["sprint"]
    summary = report.get("summary") or {}
    print(f"Проект: {report.get('project')}")
    print(
        f"Спринт: {sprint.get('name')} (id={sprint.get('id')}, "
        f"state={sprint.get('state')})"
    )
    print(f"Период: {sprint.get('startDate', '?')} — {sprint.get('endDate', '?')}")
    print(f"Задач: {summary.get('total_issues')}")
    print("По статусам:")
    for status, count in sorted(
        (summary.get("by_status") or {}).items(), key=lambda item: (-item[1], item[0])
    ):
        print(f"  - {status}: {count}")

    confluence = report.get("confluence") or {}
    if confluence.get("ok"):
        sections = ", ".join((confluence.get("sections") or {}).keys()) or "без распознанных разделов"
        print(f"Confluence: {confluence.get('title')} ({sections})")
    else:
        print(f"Confluence: нет — {confluence.get('reason')}")

    notes = report.get("local_notes") or {}
    if notes.get("ok"):
        print(f"Локальные заметки: {notes.get('directory')} ({len(notes.get('files') or [])} файлов)")
    else:
        print(f"Локальные заметки: нет — {notes.get('reason')}")

    missing = report.get("missing_sources") or []
    if missing:
        print(f"Не хватило источников: {', '.join(missing)}")
    candidates = report.get("composition_candidates") or {}
    groups = candidates.get("groups") or []
    if groups:
        print(f"Кандидаты композиции: {len(groups)} групп (не только цели)")
        for group in groups[:12]:
            print(f"  - {group.get('id')}: {group.get('title')} [{group.get('suggested_role')}]")
    for note in report.get("source_notes") or []:
        print(f"Заметка: {note}")
    if report.get("reused_from"):
        print(f"Кэш: {report['reused_from']}")
