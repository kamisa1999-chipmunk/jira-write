"""Analyse merge requests and attach them to an issue history report."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from models.issue import parse_jira_datetime

REMARK_CATEGORIES: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "корректность",
        (
            "bug",
            "ошибк",
            "неверн",
            "incorrect",
            "wrong",
            "null",
            "npe",
            "exception",
            "crash",
            "слома",
            "баг",
            "регресс",
            "regress",
            "edge case",
            "краевой",
        ),
    ),
    (
        "архитектура",
        (
            "архитектур",
            "abstraction",
            "абстракц",
            "coupling",
            "связност",
            "solid",
            "паттерн",
            "pattern",
            "design",
            "слой",
            "layer",
            "ответственност",
            "переиспользу",
        ),
    ),
    (
        "безопасность",
        (
            "security",
            "безопасн",
            "xss",
            "csrf",
            "inject",
            "sql",
            "token",
            "секрет",
            "secret",
            "auth",
            "permission",
            "права",
            "уязвим",
        ),
    ),
    (
        "производительность",
        (
            "performance",
            "производител",
            "n+1",
            "slow",
            "медлен",
            "оптимиз",
            "memory",
            "памят",
            "cache",
            "кэш",
            "latency",
            "timeout",
        ),
    ),
    (
        "тесты",
        (
            "test",
            "тест",
            "coverage",
            "покрыт",
            "spec",
            "jest",
            "unit",
            "e2e",
            "pytest",
            "mock",
            "мок",
        ),
    ),
    (
        "читаемость и стиль",
        (
            "style",
            "стил",
            "naming",
            "нейминг",
            "читаем",
            "lint",
            "формат",
            "rename",
            "typo",
            "опечат",
            "clean",
            "clarity",
            "понятн",
        ),
    ),
    (
        "требования и бизнес-логика",
        (
            "требован",
            "бизнес",
            "acceptance",
            "продукт",
            "логика",
            "spec",
            "по тз",
            "ожидаем",
            "requirement",
            "ac ",
            "usecase",
            "сценари",
        ),
    ),
]


def attach_mr_analysis(
    report: Dict[str, Any],
    mr_bundle: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge MR timeline events and analysis blocks into a history report."""
    if mr_bundle is None:
        return report

    analysis = build_mr_analysis(mr_bundle)
    report["git"] = {
        "enabled": True,
        "ok": bool(mr_bundle.get("ok")),
        "provider": mr_bundle.get("provider"),
        "reason": mr_bundle.get("reason"),
        "load_errors": mr_bundle.get("load_errors"),
    }
    report["merge_requests"] = analysis.get("merge_requests") or []
    report["mr_analysis"] = analysis

    if analysis.get("merge_requests"):
        events = list(_iter_jira_timeline_events(report.get("timeline") or []))
        events.extend(analysis.get("timeline_events") or [])
        events.sort(key=lambda e: e.get("at") or "")
        report["timeline"] = _regroup_timeline(events)

    return report


def build_mr_analysis(mr_bundle: Dict[str, Any]) -> Dict[str, Any]:
    mrs_out: List[Dict[str, Any]] = []
    timeline_events: List[Dict[str, Any]] = []

    for mr in mr_bundle.get("merge_requests") or []:
        analyzed = _analyse_single_mr(mr)
        mrs_out.append(analyzed)
        timeline_events.extend(analyzed.get("timeline_events") or [])

    timeline_events.sort(key=lambda e: e.get("at") or "")

    return {
        "ok": bool(mr_bundle.get("ok")),
        "reason": mr_bundle.get("reason"),
        "provider": mr_bundle.get("provider"),
        "merge_requests": mrs_out,
        "timeline_events": timeline_events,
    }


def render_mr_markdown(analysis: Optional[Dict[str, Any]], git_meta: Optional[Dict[str, Any]] = None) -> str:
    lines: List[str] = ["## Merge Request", ""]

    if git_meta and not git_meta.get("ok"):
        lines.append(f"Git-анализ недоступен: {git_meta.get('reason') or 'неизвестная причина'}")
        lines.append("")
        return "\n".join(lines)

    if not analysis or not analysis.get("merge_requests"):
        reason = (analysis or {}).get("reason") or (git_meta or {}).get("reason")
        lines.append(reason or "Связанные MR не найдены")
        lines.append("")
        return "\n".join(lines)

    for mr in analysis["merge_requests"]:
        ref = mr.get("ref") or f"{mr.get('project')}!{mr.get('iid')}"
        title = mr.get("title") or ""
        lines.append(f"### {ref} — {title}")
        lines.append("")
        if mr.get("url"):
            lines.append(f"- Ссылка: {mr['url']}")
        lines.append(f"- Автор: {mr.get('author') or '—'}")
        lines.append(f"- Статус: {mr.get('status') or '—'}")
        lines.append(f"- Создан: {_fmt(mr.get('created_at'))}")
        if mr.get("merged_at"):
            lines.append(f"- Влит: {_fmt(mr.get('merged_at'))}")
        elif mr.get("closed_at"):
            lines.append(f"- Закрыт: {_fmt(mr.get('closed_at'))}")
        else:
            lines.append("- Влит: —")
        lines.append(f"- Время до первого ревью: {_fmt_duration(mr.get('time_to_first_review'))}")
        lines.append(
            f"- Время от первого замечания до исправления: "
            f"{_fmt_duration(mr.get('time_remark_to_fix'))}"
        )
        reviewers = ", ".join(mr.get("reviewers") or []) or "—"
        lines.append(f"- Reviewers: {reviewers}")
        lines.append(
            f"- Замечаний: {mr.get('remarks_total', 0)} "
            f"(открытых {mr.get('remarks_open', 0)}, закрытых {mr.get('remarks_closed', 0)})"
        )
        lines.append(
            f"- Циклов исправление → повторное ревью: {mr.get('fix_cycles', 0)}"
        )

        categories = mr.get("remark_categories") or {}
        if categories:
            lines.append("- Классификация замечаний:")
            for name, count in categories.items():
                if count:
                    lines.append(f"  - {name}: {count}")

        behavior = mr.get("author_behavior") or {}
        facts = behavior.get("facts") or []
        observations = behavior.get("observations") or []
        questions = behavior.get("questions") or []
        if facts or observations or questions:
            lines.append("- Поведение автора (только по фактам этого MR):")
            if facts:
                lines.append("  - Факты:")
                for item in facts:
                    lines.append(f"    - {item}")
            if observations:
                lines.append("  - Наблюдения:")
                for item in observations:
                    lines.append(f"    - {item}")
            if questions:
                lines.append("  - Возможные вопросы для обсуждения:")
                for item in questions:
                    lines.append(f"    - {item}")
        lines.append("")

    return "\n".join(lines)


def _analyse_single_mr(mr: Dict[str, Any]) -> Dict[str, Any]:
    created = mr.get("created_at")
    first_review = mr.get("first_review_at")
    first_remark = mr.get("first_remark_at")
    first_fix = mr.get("first_fix_after_remark_at")
    approved = mr.get("approved_at")
    merged = mr.get("merged_at")
    closed = mr.get("closed_at")

    categories = _classify_remarks(mr.get("discussions") or [])
    behavior = _author_behavior(mr)

    timeline_events: List[Dict[str, Any]] = []
    ref = mr.get("ref") or f"{mr.get('project')}!{mr.get('iid')}"
    author = mr.get("author") or "—"

    if created:
        timeline_events.append(
            _evt(created, author, "mr_created", f"Создан MR {ref}: {mr.get('title') or ''}".strip())
        )
    if first_review:
        timeline_events.append(
            _evt(first_review, "reviewer", "mr_review_started", f"Началось ревью MR {ref}")
        )
    if first_remark:
        timeline_events.append(
            _evt(first_remark, "reviewer", "mr_remarks", f"Появились замечания в MR {ref}")
        )
    if first_fix:
        timeline_events.append(
            _evt(first_fix, author, "mr_fixes", f"Автор внёс исправления в MR {ref}")
        )
    if approved:
        who = ", ".join(mr.get("approvals") or []) or "reviewer"
        timeline_events.append(
            _evt(approved, who, "mr_approved", f"MR {ref} получил approval")
        )
    if merged:
        timeline_events.append(
            _evt(merged, author, "mr_merged", f"MR {ref} был влит")
        )
    elif closed:
        timeline_events.append(
            _evt(closed, author, "mr_closed", f"MR {ref} был закрыт")
        )

    # Strip internal fields from public payload
    public = {
        "provider": mr.get("provider"),
        "ref": ref,
        "project": mr.get("project"),
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "url": mr.get("url"),
        "author": mr.get("author"),
        "status": mr.get("status"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "created_at": created,
        "first_review_at": first_review,
        "first_remark_at": first_remark,
        "first_fix_after_remark_at": first_fix,
        "approved_at": approved,
        "merged_at": merged,
        "closed_at": closed,
        "time_to_first_review": _hours_between(created, first_review),
        "time_remark_to_fix": _hours_between(first_remark, first_fix),
        "reviewers": mr.get("reviewers") or [],
        "approvals": mr.get("approvals") or [],
        "commits": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "author": c.get("author"),
                "authored_date": c.get("authored_date"),
            }
            for c in (mr.get("commits") or [])
        ],
        "diff_stats": mr.get("diff_stats") or {},
        "remarks_open": mr.get("remarks_open", 0),
        "remarks_closed": mr.get("remarks_closed", 0),
        "remarks_total": mr.get("remarks_total", 0),
        "fix_cycles": mr.get("fix_cycles", 0),
        "remark_categories": categories,
        "author_behavior": behavior,
        "discovery_source": mr.get("discovery_source"),
        "confidence": mr.get("confidence"),
        "timeline_events": timeline_events,
        "discussions_preview": [
            {
                "at": d.get("at"),
                "author": d.get("author"),
                "body": _preview(d.get("body") or "", 200),
                "category": _classify_one(d.get("body") or ""),
                "resolved": d.get("resolved"),
            }
            for d in (mr.get("discussions") or [])[:40]
        ],
    }
    return public


def _classify_remarks(discussions: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {name: 0 for name, _ in REMARK_CATEGORIES}
    counts["прочее"] = 0
    for item in discussions:
        cat = _classify_one(item.get("body") or "")
        counts[cat] = counts.get(cat, 0) + 1
    return {k: v for k, v in counts.items() if v > 0}


def _classify_one(body: str) -> str:
    text = body.lower()
    for name, keywords in REMARK_CATEGORIES:
        if any(k in text for k in keywords):
            return name
    return "прочее"


def _author_behavior(mr: Dict[str, Any]) -> Dict[str, List[str]]:
    facts: List[str] = []
    observations: List[str] = []
    questions: List[str] = []

    remarks_total = mr.get("remarks_total") or 0
    open_count = mr.get("remarks_open") or 0
    closed_count = mr.get("remarks_closed") or 0
    fix_cycles = mr.get("fix_cycles") or 0
    time_fix = _hours_between(mr.get("first_remark_at"), mr.get("first_fix_after_remark_at"))
    commits = mr.get("commits") or []

    facts.append(f"Замечаний ревьюеров: {remarks_total} (открытых {open_count}, закрытых {closed_count})")
    facts.append(f"Циклов исправление → повторное ревью: {fix_cycles}")

    if time_fix is not None:
        facts.append(f"От первого замечания до первого коммита-исправления: {_fmt_duration(time_fix)}")
    elif remarks_total and not mr.get("first_fix_after_remark_at"):
        facts.append("После замечаний не видно коммитов-исправлений в этом MR")

    if open_count == 0 and remarks_total > 0:
        facts.append("Все учтённые замечания закрыты / resolved")
    elif open_count > 0:
        facts.append(f"Осталось открытых замечаний: {open_count}")

    # Clarifying questions by author in discussions
    author_name = (mr.get("author") or "").lower()
    author_username = (mr.get("author_username") or "").lower()
    author_questions = 0
    for d in mr.get("discussions") or []:
        who = (d.get("username") or d.get("author") or "").lower()
        if who and (who == author_username or who == author_name or author_username in who):
            body = d.get("body") or ""
            if "?" in body or "уточн" in body.lower() or "вопрос" in body.lower():
                author_questions += 1
    # Author replies are mostly not in remarks list (reviewer-only). Check notes meta if present.
    meta = mr.get("_notes_meta") or {}
    if meta.get("author_reply_times"):
        facts.append(f"Ответов автора в обсуждениях: {len(meta['author_reply_times'])}")
    if author_questions:
        facts.append(f"Уточняющих вопросов автора в тредах: {author_questions}")

    # Repeated remark themes
    cats = _classify_remarks(mr.get("discussions") or [])
    repeated = [name for name, count in cats.items() if count >= 2]
    if repeated:
        facts.append(f"Повторяющиеся темы замечаний: {', '.join(repeated)}")

    # Commit clarity after remarks
    fix_commits = []
    first_remark = mr.get("first_remark_at")
    if first_remark:
        for c in commits:
            when = c.get("authored_date")
            if when and when > first_remark:
                fix_commits.append(c)
    if fix_commits:
        titles = [c.get("title") or "" for c in fix_commits]
        separate = len(fix_commits) >= 2
        facts.append(f"Коммитов после первого замечания: {len(fix_commits)}")
        if separate:
            observations.append(
                "Исправления пришли отдельными коммитами: "
                + "; ".join(t for t in titles[:5] if t)
            )
        else:
            observations.append(
                f"После замечаний один коммит: {titles[0] if titles else '—'}"
            )

    if time_fix is not None:
        if time_fix <= 4:
            observations.append("Автор быстро отреагировал на первое замечание (≤ 4 ч)")
        elif time_fix <= 24:
            observations.append("Автор ответил на замечание в пределах суток")
        else:
            observations.append("Между замечанием и исправлением прошло больше суток")

    if fix_cycles >= 2:
        observations.append("Потребовалось несколько циклов ревью")
    elif fix_cycles == 1 and remarks_total:
        observations.append("Хватило одного цикла исправлений")

    if remarks_total:
        questions.append("Что из замечаний было неожиданным, а что — ожидаемой доработкой?")
    if fix_cycles >= 2:
        questions.append("Какие замечания повторялись между циклами и как их предотвратить заранее?")
    if open_count > 0:
        questions.append("Почему часть обсуждений осталась открытой?")

    return {
        "facts": facts,
        "observations": observations,
        "questions": questions,
    }


def _evt(at: str, author: str, kind: str, text: str) -> Dict[str, Any]:
    return {"at": at, "author": author, "kind": kind, "text": text}


def _iter_jira_timeline_events(timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for group in timeline:
        at = group.get("at")
        author = group.get("author")
        for event in group.get("events") or []:
            events.append(
                {
                    "at": at,
                    "author": author,
                    "kind": event.get("kind"),
                    "text": event.get("text"),
                    **{
                        k: event[k]
                        for k in ("from", "to", "field", "is_return")
                        if k in event
                    },
                }
            )
    return events


def _regroup_timeline(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not events:
        return []

    groups: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for event in events:
        at = event.get("at") or ""
        author = event.get("author") or "—"
        bucket = _time_bucket(at)

        public = {"kind": event.get("kind"), "text": event.get("text")}
        for key in ("from", "to", "field", "is_return"):
            if key in event:
                public[key] = event[key]

        if (
            current is not None
            and current.get("_bucket") == bucket
            and current.get("author") == author
        ):
            current["events"].append(public)
            continue

        current = {
            "at": at,
            "author": author,
            "_bucket": bucket,
            "events": [public],
        }
        groups.append(current)

    for group in groups:
        group.pop("_bucket", None)
    return groups


def _time_bucket(value: str) -> str:
    try:
        dt = parse_jira_datetime(value)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return value[:16] if value else ""


def _hours_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        a = parse_jira_datetime(start)
        b = parse_jira_datetime(end)
    except Exception:
        return None
    if a.tzinfo and b.tzinfo is None:
        b = b.replace(tzinfo=a.tzinfo)
    if b.tzinfo and a.tzinfo is None:
        a = a.replace(tzinfo=b.tzinfo)
    return round((b - a).total_seconds() / 3600.0, 1)


def _fmt(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return parse_jira_datetime(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def _fmt_duration(hours: Optional[float]) -> str:
    if hours is None:
        return "—"
    if hours < 24:
        return f"{hours} ч"
    days = round(hours / 24.0, 1)
    return f"{days} дн. ({hours} ч)"


def _preview(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
