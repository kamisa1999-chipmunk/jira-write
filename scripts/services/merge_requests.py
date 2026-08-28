"""Find and enrich merge requests linked to a Jira issue."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from git_client import GitError, GitHubClient, GitLabClient
from jira_client import JiraClient, JiraError

logger = logging.getLogger(__name__)

# Explicit MR/PR URLs — not free-text key mentions.
GITLAB_MR_URL_RE = re.compile(
    r"https?://[^\s\]\|\"'<>]+/-/merge_requests/(\d+)",
    re.IGNORECASE,
)
GITHUB_PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)",
    re.IGNORECASE,
)

BOT_NAME_MARKERS = (
    "bot",
    "gitlab-developers",
    "dependabot",
    "renovate",
    "danger",
    "semantic-release",
    "codecov",
    "sonarcloud",
    "jenkins",
    "github-actions",
)


def discover_mr_refs_from_jira(
    *,
    issue_key: str,
    raw_issue: Dict[str, Any],
    jira_client: JiraClient,
) -> List[Dict[str, Any]]:
    """Find MR refs via Jira only (dev-status, remotelinks, comment/changelog URLs).

    Does not call Git API. Useful for counting related MRs before a full git analysis.
    """
    found: Dict[str, Dict[str, Any]] = {}

    def add(ref: Dict[str, Any]) -> None:
        key = ref.get("ref")
        if not key:
            return
        existing = found.get(key)
        if existing is None or _source_rank(ref["source"]) > _source_rank(existing["source"]):
            found[key] = ref

    for ref in _refs_from_jira_dev_status(jira_client, raw_issue):
        add(ref)
    for ref in _refs_from_jira_remotelinks(jira_client, issue_key):
        add(ref)
    for ref in _refs_from_jira_text(raw_issue, issue_key):
        add(ref)

    confident = [
        ref
        for ref in found.values()
        if ref.get("confidence") in {"high", "medium"}
    ]
    confident.sort(key=lambda r: (-_source_rank(r["source"]), r.get("ref") or ""))
    return confident


def collect_merge_requests(
    *,
    issue_key: str,
    raw_issue: Dict[str, Any],
    jira_client: JiraClient,
    git_client: Any,
) -> Dict[str, Any]:
    """Discover related MRs and return enriched analysis payload.

    Never raises for missing MRs — returns `ok=False` with a reason instead.
    """
    refs = _discover_mr_refs(
        issue_key=issue_key,
        raw_issue=raw_issue,
        jira_client=jira_client,
        git_client=git_client,
    )

    if not refs:
        return {
            "ok": True,
            "provider": getattr(git_client, "provider", None),
            "reason": f"Связанные MR для {issue_key} не найдены",
            "merge_requests": [],
        }

    enriched: List[Dict[str, Any]] = []
    errors: List[str] = []

    for ref in refs:
        try:
            if isinstance(git_client, GitLabClient):
                mr = _enrich_gitlab_mr(git_client, ref, issue_key)
            elif isinstance(git_client, GitHubClient):
                mr = _enrich_github_pr(git_client, ref, issue_key)
            else:
                raise GitError(f"Неизвестный Git-клиент: {type(git_client)}")
            if mr:
                enriched.append(mr)
        except GitError as exc:
            errors.append(f"{ref.get('ref')}: {exc}")
            logger.warning("MR enrich failed for %s: %s", ref.get("ref"), exc)

    if not enriched:
        reason = "Не удалось загрузить MR из Git API"
        if errors:
            reason = f"{reason}: {'; '.join(errors[:3])}"
        return {
            "ok": False,
            "provider": getattr(git_client, "provider", None),
            "reason": reason,
            "merge_requests": [],
        }

    return {
        "ok": True,
        "provider": getattr(git_client, "provider", None),
        "reason": None,
        "merge_requests": enriched,
        "load_errors": errors or None,
    }


def _discover_mr_refs(
    *,
    issue_key: str,
    raw_issue: Dict[str, Any],
    jira_client: JiraClient,
    git_client: Any,
) -> List[Dict[str, Any]]:
    """Collect unique MR refs with confident linkage to the issue key."""
    found: Dict[str, Dict[str, Any]] = {}

    def add(ref: Dict[str, Any]) -> None:
        key = ref.get("ref")
        if not key:
            return
        existing = found.get(key)
        if existing is None or _source_rank(ref["source"]) > _source_rank(existing["source"]):
            found[key] = ref

    # 1) Jira development / remote links
    for ref in _refs_from_jira_dev_status(jira_client, raw_issue):
        add(ref)
    for ref in _refs_from_jira_remotelinks(jira_client, issue_key):
        add(ref)
    for ref in _refs_from_jira_text(raw_issue, issue_key):
        add(ref)

    # 2) Git search by issue key (title / branch)
    try:
        if isinstance(git_client, GitLabClient):
            for ref in _refs_from_gitlab_search(git_client, issue_key):
                add(ref)
        elif isinstance(git_client, GitHubClient):
            for ref in _refs_from_github_search(git_client, issue_key):
                add(ref)
    except GitError as exc:
        logger.warning("Git search failed for %s: %s", issue_key, exc)

    # Keep only confident matches
    confident = [
        ref
        for ref in found.values()
        if ref.get("confidence") in {"high", "medium"}
    ]
    confident.sort(key=lambda r: (-_source_rank(r["source"]), r.get("ref") or ""))
    return confident


def _source_rank(source: str) -> int:
    order = {
        "development_link": 50,
        "remote_link": 40,
        "comment_url": 35,
        "changelog_url": 30,
        "title_branch": 20,
        "search_title": 15,
        "search_weak": 5,
    }
    return order.get(source, 0)


def _refs_from_jira_dev_status(
    jira_client: JiraClient, raw_issue: Dict[str, Any]
) -> List[Dict[str, Any]]:
    issue_id = raw_issue.get("id")
    if not issue_id:
        return []

    refs: List[Dict[str, Any]] = []
    for app_type in ("gitlab", "GitLab", "github", "GitHub", "stash", "bitbucket"):
        for data_type in ("pullrequest", "pullRequest"):
            try:
                data = jira_client.get(
                    "/rest/dev-status/1.0/issue/detail",
                    params={
                        "issueId": issue_id,
                        "applicationType": app_type,
                        "dataType": data_type,
                    },
                )
            except JiraError:
                continue
            for detail in (data or {}).get("detail") or []:
                for pr in detail.get("pullRequests") or []:
                    url = pr.get("url") or ""
                    parsed = _parse_mr_url(url)
                    if not parsed:
                        continue
                    refs.append(
                        {
                            **parsed,
                            "source": "development_link",
                            "confidence": "high",
                            "title": pr.get("name") or pr.get("title"),
                            "status": pr.get("status"),
                        }
                    )
    return refs


def _refs_from_jira_remotelinks(
    jira_client: JiraClient, issue_key: str
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    try:
        links = jira_client.get(f"/rest/api/2/issue/{issue_key}/remotelink")
    except JiraError:
        return []

    for link in links or []:
        obj = link.get("object") or {}
        url = obj.get("url") or ""
        title = obj.get("title") or ""
        parsed = _parse_mr_url(url)
        if not parsed:
            # Sometimes only title mentions MR without usable path
            continue
        refs.append(
            {
                **parsed,
                "source": "remote_link",
                "confidence": "high",
                "title": title or None,
            }
        )
    return refs


def _refs_from_jira_text(
    raw_issue: Dict[str, Any], issue_key: str
) -> List[Dict[str, Any]]:
    """Extract explicit MR URLs from comments and changelog strings."""
    refs: List[Dict[str, Any]] = []
    fields = raw_issue.get("fields") or {}

    comment_block = fields.get("comment") or {}
    for comment in comment_block.get("comments") or []:
        body = comment.get("body")
        text = body if isinstance(body, str) else str(body or "")
        for parsed in _extract_mr_urls(text):
            refs.append({**parsed, "source": "comment_url", "confidence": "high"})

    for history in (raw_issue.get("changelog") or {}).get("histories") or []:
        for item in history.get("items") or []:
            blob = f"{item.get('fromString') or ''} {item.get('toString') or ''}"
            for parsed in _extract_mr_urls(blob):
                refs.append({**parsed, "source": "changelog_url", "confidence": "high"})

    return refs


def _refs_from_gitlab_search(
    client: GitLabClient, issue_key: str
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    candidates: List[Dict[str, Any]] = []
    try:
        candidates.extend(client.search_merge_requests(issue_key, per_page=30))
    except GitError:
        pass
    try:
        candidates.extend(client.list_merge_requests(search=issue_key, per_page=50))
    except GitError:
        pass

    for mr in candidates:
        project_id = mr.get("project_id")
        iid = mr.get("iid")
        if project_id is None or iid is None:
            continue
        ref_key = f"gitlab:{project_id}!{iid}"
        if ref_key in seen:
            continue
        seen.add(ref_key)

        title = mr.get("title") or ""
        branch = mr.get("source_branch") or ""
        conf, source = _match_issue_key(issue_key, title=title, branch=branch, description=mr.get("description") or "")
        if conf == "reject":
            continue

        web_url = mr.get("web_url") or ""
        project_path = None
        parsed = _parse_mr_url(web_url)
        if parsed:
            project_path = parsed.get("project")

        refs.append(
            {
                "provider": "gitlab",
                "ref": ref_key,
                "project_id": project_id,
                "project": project_path,
                "iid": int(iid),
                "url": web_url or None,
                "source": source,
                "confidence": conf,
                "title": title,
            }
        )
    return refs


def _refs_from_github_search(
    client: GitHubClient, issue_key: str
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for item in client.search_pull_requests(issue_key, per_page=20):
        repo = (item.get("repository_url") or "").rstrip("/").split("/")
        if len(repo) < 2:
            continue
        owner, name = repo[-2], repo[-1]
        number = item.get("number")
        if not number:
            continue
        title = item.get("title") or ""
        conf, source = _match_issue_key(
            issue_key,
            title=title,
            branch="",
            description=item.get("body") or "",
        )
        if conf == "reject":
            continue
        refs.append(
            {
                "provider": "github",
                "ref": f"github:{owner}/{name}#{number}",
                "owner": owner,
                "repo": name,
                "project": f"{owner}/{name}",
                "iid": int(number),
                "url": item.get("html_url"),
                "source": source,
                "confidence": conf,
                "title": title,
            }
        )
    return refs


def _match_issue_key(
    issue_key: str,
    *,
    title: str,
    branch: str,
    description: str,
) -> Tuple[str, str]:
    """Return (confidence, source). Reject accidental text-only hits."""
    key = issue_key.upper()
    title_u = title.upper()
    branch_u = branch.upper()
    desc_u = description.upper()

    key_in_title = bool(re.search(rf"\b{re.escape(key)}\b", title_u))
    key_in_branch = bool(re.search(rf"\b{re.escape(key)}\b", branch_u))
    key_in_desc = bool(re.search(rf"\b{re.escape(key)}\b", desc_u))

    if key_in_title or key_in_branch:
        return "medium", "title_branch"
    # Description-only without title/branch — too easy to be a false positive
    if key_in_desc and not key_in_title and not key_in_branch:
        return "reject", "search_weak"
    return "reject", "search_weak"


def _extract_mr_urls(text: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for match in GITLAB_MR_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,;\"'")
        parsed = _parse_mr_url(url)
        if parsed:
            results.append(parsed)
    for match in GITHUB_PR_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,;\"'")
        parsed = _parse_mr_url(url)
        if parsed:
            results.append(parsed)
    return results


def _parse_mr_url(url: str) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    url = url.strip().rstrip(").,;\"'")

    gh = GITHUB_PR_URL_RE.search(url)
    if gh:
        owner, repo, number = gh.group(1), gh.group(2), int(gh.group(3))
        return {
            "provider": "github",
            "ref": f"github:{owner}/{repo}#{number}",
            "owner": owner,
            "repo": repo,
            "project": f"{owner}/{repo}",
            "iid": number,
            "url": f"https://github.com/{owner}/{repo}/pull/{number}",
        }

    gl = GITLAB_MR_URL_RE.search(url)
    if not gl:
        return None

    iid = int(gl.group(1))
    parsed = urlparse(url)
    path = unquote(parsed.path or "")
    marker = "/-/merge_requests/"
    if marker not in path:
        return None
    project_path = path.split(marker, 1)[0].lstrip("/")
    if not project_path:
        return None

    return {
        "provider": "gitlab",
        "ref": f"gitlab:{project_path}!{iid}",
        "project": project_path,
        "project_id": project_path,
        "iid": iid,
        "url": f"{parsed.scheme}://{parsed.netloc}/{project_path}/-/merge_requests/{iid}",
    }


def _enrich_gitlab_mr(
    client: GitLabClient, ref: Dict[str, Any], issue_key: str
) -> Optional[Dict[str, Any]]:
    project_id = ref.get("project_id") or ref.get("project")
    iid = ref.get("iid")
    if not project_id or iid is None:
        return None

    mr = client.get_merge_request(str(project_id), int(iid))
    title = mr.get("title") or ""
    branch = mr.get("source_branch") or ""

    # Final confidence gate for search-sourced refs
    if ref.get("source") in {"title_branch", "search_title", "search_weak"}:
        conf, _ = _match_issue_key(
            issue_key,
            title=title,
            branch=branch,
            description=mr.get("description") or "",
        )
        if conf == "reject" and ref.get("confidence") != "high":
            return None

    commits = client.get_merge_request_commits(str(project_id), int(iid))
    discussions = client.get_merge_request_discussions(str(project_id), int(iid))

    changes: Dict[str, Any] = {}
    try:
        changes = client.get_merge_request_changes(str(project_id), int(iid))
    except GitError as exc:
        logger.debug("MR changes unavailable: %s", exc)

    approvals: Dict[str, Any] = {}
    try:
        approvals = client.get_merge_request_approvals(str(project_id), int(iid))
    except GitError as exc:
        logger.debug("MR approvals unavailable: %s", exc)

    author = ((mr.get("author") or {}).get("name") or (mr.get("author") or {}).get("username"))
    web_url = mr.get("web_url") or ref.get("url")
    project_path = ref.get("project")
    if not project_path and web_url:
        parsed = _parse_mr_url(web_url)
        if parsed:
            project_path = parsed.get("project")

    files = changes.get("changes") or []
    changes_count = mr.get("changes_count")
    if changes_count is not None:
        try:
            changes_count = int(changes_count)
        except (TypeError, ValueError):
            pass
    diff_stats = {
        "files_changed": changes_count if changes_count is not None else (len(files) or None),
        "additions": mr.get("additions"),
        "deletions": mr.get("deletions"),
    }

    reviewers = []
    for user in mr.get("reviewers") or []:
        reviewers.append(user.get("name") or user.get("username"))
    approved_by = []
    for entry in (approvals.get("approved_by") or []):
        user = entry.get("user") or entry
        approved_by.append(user.get("name") or user.get("username"))

    notes = _flatten_gitlab_discussions(discussions, author_username=(mr.get("author") or {}).get("username"))

    return {
        "provider": "gitlab",
        "ref": f"{project_path or project_id}!{iid}",
        "project": project_path or str(project_id),
        "iid": int(iid),
        "title": title,
        "url": web_url,
        "author": author,
        "author_username": (mr.get("author") or {}).get("username"),
        "status": _gitlab_status(mr),
        "source_branch": branch,
        "target_branch": mr.get("target_branch"),
        "created_at": mr.get("created_at"),
        "updated_at": mr.get("updated_at"),
        "merged_at": mr.get("merged_at"),
        "closed_at": mr.get("closed_at"),
        "first_review_at": notes["first_review_at"],
        "first_remark_at": notes["first_remark_at"],
        "first_fix_after_remark_at": _first_fix_after(
            commits, notes["first_remark_at"], author_username=(mr.get("author") or {}).get("username")
        ),
        "approved_at": notes.get("first_approval_at") or _approvals_timestamp(approvals),
        "reviewers": sorted({r for r in reviewers if r}),
        "approvals": sorted({a for a in approved_by if a}),
        "commits": [
            {
                "id": (c.get("id") or "")[:12],
                "title": c.get("title"),
                "author": c.get("author_name") or c.get("committer_name"),
                "authored_date": c.get("authored_date") or c.get("created_at"),
                "message": c.get("message"),
            }
            for c in commits
        ],
        "diff_stats": diff_stats,
        "discussions": notes["remarks"],
        "remarks_open": notes["open_count"],
        "remarks_closed": notes["closed_count"],
        "remarks_total": notes["open_count"] + notes["closed_count"],
        "fix_cycles": notes["fix_cycles"],
        "discovery_source": ref.get("source"),
        "confidence": ref.get("confidence"),
        # keep raw-ish for analysis helpers
        "_commits_raw": commits,
        "_notes_meta": notes,
    }


def _enrich_github_pr(
    client: GitHubClient, ref: Dict[str, Any], issue_key: str
) -> Optional[Dict[str, Any]]:
    owner = ref.get("owner")
    repo = ref.get("repo")
    number = ref.get("iid")
    if not owner or not repo or number is None:
        return None

    pr = client.get_pull_request(owner, repo, int(number))
    title = pr.get("title") or ""
    branch = (pr.get("head") or {}).get("ref") or ""

    if ref.get("source") in {"title_branch", "search_title", "search_weak"}:
        conf, _ = _match_issue_key(
            issue_key,
            title=title,
            branch=branch,
            description=pr.get("body") or "",
        )
        if conf == "reject" and ref.get("confidence") != "high":
            return None

    commits = client.get_pull_commits(owner, repo, int(number))
    files = client.get_pull_files(owner, repo, int(number))
    reviews = client.get_pull_reviews(owner, repo, int(number))
    review_comments = client.get_pull_review_comments(owner, repo, int(number))
    issue_comments = client.get_issue_comments(owner, repo, int(number))

    author = ((pr.get("user") or {}).get("login"))
    notes = _flatten_github_notes(
        reviews=reviews,
        review_comments=review_comments,
        issue_comments=issue_comments,
        author_login=author,
    )

    approved_by = [
        (r.get("user") or {}).get("login")
        for r in reviews
        if (r.get("state") or "").upper() == "APPROVED"
    ]
    reviewers = [
        (r.get("user") or {}).get("login")
        for r in reviews
        if (r.get("user") or {}).get("login")
    ]

    status = "merged" if pr.get("merged_at") else (pr.get("state") or "open")
    additions = sum(int(f.get("additions") or 0) for f in files)
    deletions = sum(int(f.get("deletions") or 0) for f in files)

    return {
        "provider": "github",
        "ref": f"{owner}/{repo}#{number}",
        "project": f"{owner}/{repo}",
        "iid": int(number),
        "title": title,
        "url": pr.get("html_url") or ref.get("url"),
        "author": author,
        "author_username": author,
        "status": status,
        "source_branch": branch,
        "target_branch": (pr.get("base") or {}).get("ref"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at") if not pr.get("merged_at") else None,
        "first_review_at": notes["first_review_at"],
        "first_remark_at": notes["first_remark_at"],
        "first_fix_after_remark_at": _first_fix_after_github(
            commits, notes["first_remark_at"], author_login=author
        ),
        "approved_at": notes.get("first_approval_at"),
        "reviewers": sorted({r for r in reviewers if r}),
        "approvals": sorted({a for a in approved_by if a}),
        "commits": [
            {
                "id": (c.get("sha") or "")[:12],
                "title": ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name")
                or ((c.get("author") or {}).get("login")),
                "authored_date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                "message": (c.get("commit") or {}).get("message"),
            }
            for c in commits
        ],
        "diff_stats": {
            "files_changed": len(files),
            "additions": additions,
            "deletions": deletions,
        },
        "discussions": notes["remarks"],
        "remarks_open": notes["open_count"],
        "remarks_closed": notes["closed_count"],
        "remarks_total": notes["open_count"] + notes["closed_count"],
        "fix_cycles": notes["fix_cycles"],
        "discovery_source": ref.get("source"),
        "confidence": ref.get("confidence"),
        "_commits_raw": commits,
        "_notes_meta": notes,
    }


def _gitlab_status(mr: Dict[str, Any]) -> str:
    if mr.get("merged_at") or mr.get("state") == "merged":
        return "merged"
    state = (mr.get("state") or "opened").lower()
    if state == "opened":
        return "opened" if not mr.get("draft") and not mr.get("work_in_progress") else "draft"
    return state


def _is_bot_name(name: Optional[str]) -> bool:
    if not name:
        return False
    lowered = name.strip().lower()
    return any(marker in lowered for marker in BOT_NAME_MARKERS)


def _flatten_gitlab_discussions(
    discussions: List[Dict[str, Any]],
    *,
    author_username: Optional[str],
) -> Dict[str, Any]:
    remarks: List[Dict[str, Any]] = []
    first_review_at: Optional[str] = None
    first_remark_at: Optional[str] = None
    first_approval_at: Optional[str] = None
    open_count = 0
    closed_count = 0
    reviewer_times: List[str] = []
    author_reply_times: List[str] = []

    for discussion in discussions or []:
        notes = discussion.get("notes") or []
        if not notes:
            continue
        resolvable = False
        resolved = False
        thread_remarks: List[Dict[str, Any]] = []

        for note in notes:
            if note.get("system"):
                body = (note.get("body") or "").lower()
                created = note.get("created_at")
                if "approved this merge request" in body and created:
                    if first_approval_at is None or created < first_approval_at:
                        first_approval_at = created
                continue

            user = note.get("author") or {}
            username = user.get("username") or ""
            display = user.get("name") or username
            if _is_bot_name(username) or _is_bot_name(display):
                continue

            body = (note.get("body") or "").strip()
            if not body:
                continue

            created = note.get("created_at")
            is_author = bool(author_username and username == author_username)

            if not is_author:
                if first_review_at is None or (created and created < first_review_at):
                    first_review_at = created
                if created:
                    reviewer_times.append(created)
                if note.get("resolvable") or note.get("type") == "DiffNote" or discussion.get("individual_note") is False:
                    if first_remark_at is None or (created and created < first_remark_at):
                        first_remark_at = created
                    thread_remarks.append(
                        {
                            "at": created,
                            "author": display,
                            "username": username,
                            "body": body,
                            "resolved": bool(note.get("resolved")),
                        }
                    )
            else:
                if created:
                    author_reply_times.append(created)

            if note.get("resolvable"):
                resolvable = True
                resolved = bool(note.get("resolved"))

        if thread_remarks:
            remarks.extend(thread_remarks)
            if resolvable:
                if resolved:
                    closed_count += 1
                else:
                    open_count += 1
            else:
                # non-resolvable human review notes still count as remarks
                closed_count += 1

    # If no resolvable threads, count unique reviewer notes as closed remarks
    if open_count + closed_count == 0 and remarks:
        closed_count = len(remarks)

    fix_cycles = _estimate_fix_cycles(reviewer_times, author_reply_times)

    return {
        "remarks": remarks,
        "open_count": open_count,
        "closed_count": closed_count,
        "first_review_at": first_review_at,
        "first_remark_at": first_remark_at,
        "first_approval_at": first_approval_at,
        "fix_cycles": fix_cycles,
        "reviewer_times": reviewer_times,
        "author_reply_times": author_reply_times,
    }


def _flatten_github_notes(
    *,
    reviews: List[Dict[str, Any]],
    review_comments: List[Dict[str, Any]],
    issue_comments: List[Dict[str, Any]],
    author_login: Optional[str],
) -> Dict[str, Any]:
    remarks: List[Dict[str, Any]] = []
    first_review_at: Optional[str] = None
    first_remark_at: Optional[str] = None
    first_approval_at: Optional[str] = None
    reviewer_times: List[str] = []
    author_reply_times: List[str] = []
    open_count = 0
    closed_count = 0

    for review in reviews or []:
        user = (review.get("user") or {}).get("login") or ""
        if _is_bot_name(user):
            continue
        state = (review.get("state") or "").upper()
        submitted = review.get("submitted_at")
        if state == "APPROVED" and submitted:
            if first_approval_at is None or submitted < first_approval_at:
                first_approval_at = submitted
        if user and author_login and user != author_login and submitted:
            if first_review_at is None or submitted < first_review_at:
                first_review_at = submitted
            reviewer_times.append(submitted)
        body = (review.get("body") or "").strip()
        if body and user and user != author_login and state in {"CHANGES_REQUESTED", "COMMENTED", "APPROVED"}:
            remarks.append(
                {
                    "at": submitted,
                    "author": user,
                    "username": user,
                    "body": body,
                    "resolved": state != "CHANGES_REQUESTED",
                }
            )
            if state == "CHANGES_REQUESTED":
                open_count += 1
                if first_remark_at is None or (submitted and submitted < first_remark_at):
                    first_remark_at = submitted
            else:
                closed_count += 1

    for comment in review_comments or []:
        user = (comment.get("user") or {}).get("login") or ""
        if _is_bot_name(user):
            continue
        body = (comment.get("body") or "").strip()
        created = comment.get("created_at")
        if not body:
            continue
        if author_login and user == author_login:
            if created:
                author_reply_times.append(created)
            continue
        if created:
            reviewer_times.append(created)
            if first_review_at is None or created < first_review_at:
                first_review_at = created
            if first_remark_at is None or created < first_remark_at:
                first_remark_at = created
        remarks.append(
            {
                "at": created,
                "author": user,
                "username": user,
                "body": body,
                "resolved": True,  # GitHub review threads resolution is limited in REST
            }
        )
        closed_count += 1

    for comment in issue_comments or []:
        user = (comment.get("user") or {}).get("login") or ""
        if _is_bot_name(user):
            continue
        created = comment.get("created_at")
        if author_login and user == author_login and created:
            author_reply_times.append(created)
        elif created:
            reviewer_times.append(created)
            if first_review_at is None or created < first_review_at:
                first_review_at = created

    return {
        "remarks": remarks,
        "open_count": open_count,
        "closed_count": closed_count,
        "first_review_at": first_review_at,
        "first_remark_at": first_remark_at,
        "first_approval_at": first_approval_at,
        "fix_cycles": _estimate_fix_cycles(reviewer_times, author_reply_times),
        "reviewer_times": reviewer_times,
        "author_reply_times": author_reply_times,
    }


def _estimate_fix_cycles(
    reviewer_times: List[str], author_reply_times: List[str]
) -> int:
    """Count reviewer→author→reviewer alternations as fix cycles."""
    if not reviewer_times or not author_reply_times:
        return 0

    events = [(t, "reviewer") for t in reviewer_times if t]
    events += [(t, "author") for t in author_reply_times if t]
    events.sort(key=lambda x: x[0])

    cycles = 0
    saw_reviewer = False
    saw_author_after = False
    for _, who in events:
        if who == "reviewer":
            if saw_reviewer and saw_author_after:
                cycles += 1
                saw_author_after = False
            saw_reviewer = True
        elif who == "author" and saw_reviewer:
            saw_author_after = True
    if saw_reviewer and saw_author_after:
        cycles += 1
    return cycles


def _first_fix_after(
    commits: List[Dict[str, Any]],
    first_remark_at: Optional[str],
    *,
    author_username: Optional[str],
) -> Optional[str]:
    if not first_remark_at:
        return None
    for commit in commits:
        when = commit.get("authored_date") or commit.get("created_at")
        if not when or when <= first_remark_at:
            continue
        # Prefer author's commits; if unknown, take first after remark
        email = (commit.get("author_email") or "").lower()
        name = (commit.get("author_name") or "").lower()
        if author_username and author_username.lower() not in name and author_username.lower() not in email:
            # still accept — GitLab commit author often differs from username
            pass
        return when
    return None


def _first_fix_after_github(
    commits: List[Dict[str, Any]],
    first_remark_at: Optional[str],
    *,
    author_login: Optional[str],
) -> Optional[str]:
    if not first_remark_at:
        return None
    for commit in commits:
        when = ((commit.get("commit") or {}).get("author") or {}).get("date")
        if not when or when <= first_remark_at:
            continue
        return when
    return None


def _approvals_timestamp(approvals: Dict[str, Any]) -> Optional[str]:
    # GitLab approvals endpoint may not expose timestamp; leave None
    if approvals.get("approved"):
        return None
    return None
