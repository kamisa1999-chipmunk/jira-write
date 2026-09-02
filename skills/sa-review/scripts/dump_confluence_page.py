#!/usr/bin/env python3
"""Dump one Confluence page (title, version, storage→text) for SA review.

Does not modify the page. Token is never printed.
Finds scripts/ from cwd (или через JIRA_WRITE_REPO_DIR).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

import os

if os.environ.get("JIRA_WRITE_REPO_DIR"):
    FALLBACKS = (Path(os.environ["JIRA_WRITE_REPO_DIR"]).expanduser(),)
else:
    FALLBACKS = ()


def find_scripts_dir() -> Path:
    cwd = Path.cwd().resolve()
    seen: list[Path] = []
    for candidate in [cwd, *cwd.parents, *FALLBACKS]:
        if candidate in seen:
            continue
        seen.append(candidate)
        for rel in ("jira/scripts", "scripts"):
            scripts = candidate / rel
            if (scripts / "confluence_client").is_dir():
                return scripts
    raise SystemExit(
        "Не найден confluence_client (scripts/). Запусти из корня jira-write "
        "или задай JIRA_WRITE_REPO_DIR=/путь/к/jira-write"
    )


SCRIPTS_DIR = find_scripts_dir()
REPO_ROOT = SCRIPTS_DIR.parent if SCRIPTS_DIR.name == "scripts" else SCRIPTS_DIR.parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from confluence_client import (  # noqa: E402
    ConfluenceClient,
    ConfluenceError,
    load_confluence_config,
)

TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
WS_RE = re.compile(r"\n{3,}")


def default_out_dir() -> Path:
    if (SCRIPTS_DIR.parent / "tmp").is_dir() or SCRIPTS_DIR.parent.name == "jira":
        return SCRIPTS_DIR.parent / "tmp"
    return REPO_ROOT / "reports" / "confluence"


def storage_to_text(value: str) -> str:
    text = value.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"</tr>", "\n", text, flags=re.I)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return WS_RE.sub("\n\n", "\n".join(lines)).strip()


def dump_page(page_id: str, out_dir: Path) -> Path:
    config = load_confluence_config()
    with ConfluenceClient(config) as client:
        page = client.get(
            f"/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,space,metadata.labels"},
        )
    title = page.get("title") or ""
    version = (page.get("version") or {}).get("number")
    when = (page.get("version") or {}).get("when")
    url = f"{config.url}/pages/viewpage.action?pageId={page_id}"
    storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    labels = [
        (item.get("name") or "")
        for item in ((page.get("metadata") or {}).get("labels") or {}).get("results")
        or []
    ]
    body = storage_to_text(storage)
    header = (
        f"TITLE: {title}\n"
        f"VERSION: {version}\n"
        f"WHEN: {when}\n"
        f"URL: {url}\n"
        f"LABELS: {', '.join(labels)}\n"
        f"\n"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dump_{page_id}.txt"
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Выгрузить страницу Confluence в текст")
    parser.add_argument("page_id", help="pageId, например 831010126")
    parser.add_argument(
        "--out-dir",
        default=str(default_out_dir()),
        help="Куда писать dump_{pageId}.txt",
    )
    args = parser.parse_args()
    page_id = args.page_id.strip()
    if not page_id.isdigit():
        raise SystemExit("pageId должен быть числом")
    try:
        path = dump_page(page_id, Path(args.out_dir))
    except ConfluenceError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(path)


if __name__ == "__main__":
    main()
