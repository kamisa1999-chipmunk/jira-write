"""Issue fetch helpers: sprint issues, issue details, JQL search, changelog."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from jira_client import JiraClient

# Fields needed for estimate/risk normalization (CAT2 custom estimate fields).
DEFAULT_ISSUE_FIELDS = (
    "summary,status,assignee,issuetype,priority,"
    "components,customfield_12201,"
    "timeoriginalestimate,timespent,timetracking,"
    "customfield_10618,customfield_11330,customfield_11331,"
    "customfield_11332,customfield_11327"
)

FULL_ISSUE_FIELDS = (
    "summary,description,status,assignee,reporter,issuetype,priority,"
    "created,updated,resolutiondate,duedate,labels,comment,issuelinks,parent,subtasks,"
    "timeoriginalestimate,timespent,timetracking,*navigable"
)


def get_sprint_issues(
    client: JiraClient,
    sprint_id: int,
    fields: str = DEFAULT_ISSUE_FIELDS,
) -> List[Dict[str, Any]]:
    """All issues in a sprint (paginated Agile API)."""
    return list(
        client.paginate(
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            results_key="issues",
            max_results=100,
            params={"fields": fields},
        )
    )


def search_issues_by_jql(
    client: JiraClient,
    jql: str,
    fields: str = FULL_ISSUE_FIELDS,
    max_results: int = 100,
    limit: Optional[int] = None,
    expand: str = "changelog",
) -> List[Dict[str, Any]]:
    """Search issues via JQL (POST /rest/api/2/search)."""
    issues: List[Dict[str, Any]] = []
    start_at = 0

    while True:
        page_size = max_results
        if limit is not None:
            remaining = limit - len(issues)
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)

        body: Dict[str, Any] = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": page_size,
            "fields": [part.strip() for part in fields.split(",") if part.strip()],
        }
        if expand:
            if isinstance(expand, str):
                body["expand"] = [part.strip() for part in expand.split(",") if part.strip()]
            else:
                body["expand"] = expand

        data = client.post(
            "/rest/api/2/search",
            json_body=body,
        )
        batch = data.get("issues", [])
        issues.extend(batch)
        if start_at + len(batch) >= data.get("total", 0):
            break
        if limit is not None and len(issues) >= limit:
            break
        start_at += len(batch)

    return issues


def get_issue_details(
    client: JiraClient,
    issue_key: str,
    fields: str = FULL_ISSUE_FIELDS,
    expand: str = "changelog",
) -> Dict[str, Any]:
    return client.get(
        f"/rest/api/2/issue/{issue_key}",
        params={"fields": fields, "expand": expand},
    )


def get_issue_changelog(client: JiraClient, issue_key: str) -> Dict[str, Any]:
    data = client.get(
        f"/rest/api/2/issue/{issue_key}",
        params={"fields": "status", "expand": "changelog"},
    )
    return data.get("changelog") or {}
