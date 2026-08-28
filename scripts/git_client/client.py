"""Generic GitLab/GitHub REST clients: auth, GET, pagination, safe logging."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import requests

from .config import GitConfig
from .exceptions import GitApiError, GitAuthError

logger = logging.getLogger(__name__)


class _BaseGitClient:
    """Shared HTTP helpers. Never logs Authorization headers or tokens."""

    def __init__(self, config: GitConfig) -> None:
        self.base_url = config.url
        self.timeout = config.timeout
        self.provider = config.provider
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._configure_auth(config.pat)

    def _configure_auth(self, pat: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "_BaseGitClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        logger.debug("Git %s %s %s params=%s", self.provider, method.upper(), path, params)

        response = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise GitAuthError(
                f"{self.provider} вернул 401: проверь токен и права доступа"
            )
        if response.status_code == 403:
            raise GitAuthError(f"{self.provider} вернул 403: нет прав на этот ресурс")
        if not response.ok:
            raise GitApiError(
                response.status_code,
                f"{self.provider} API error {response.status_code}: {response.text[:500]}",
            )

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)


class GitLabClient(_BaseGitClient):
    """GitLab REST API v4 client."""

    def _configure_auth(self, pat: str) -> None:
        self.session.headers["PRIVATE-TOKEN"] = pat

    def get_merge_request(self, project_id: str, mr_iid: int) -> Dict[str, Any]:
        encoded = quote(str(project_id), safe="")
        return self.get(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}")

    def get_merge_request_commits(
        self, project_id: str, mr_iid: int
    ) -> List[Dict[str, Any]]:
        return list(
            self.paginate(
                f"/api/v4/projects/{quote(str(project_id), safe='')}/merge_requests/{mr_iid}/commits"
            )
        )

    def get_merge_request_changes(
        self, project_id: str, mr_iid: int
    ) -> Dict[str, Any]:
        encoded = quote(str(project_id), safe="")
        return self.get(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}/changes")

    def get_merge_request_approvals(
        self, project_id: str, mr_iid: int
    ) -> Dict[str, Any]:
        encoded = quote(str(project_id), safe="")
        return self.get(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}/approvals")

    def get_merge_request_discussions(
        self, project_id: str, mr_iid: int
    ) -> List[Dict[str, Any]]:
        return list(
            self.paginate(
                f"/api/v4/projects/{quote(str(project_id), safe='')}/merge_requests/{mr_iid}/discussions"
            )
        )

    def search_merge_requests(self, query: str, per_page: int = 20) -> List[Dict[str, Any]]:
        """Global MR search by text (title/description)."""
        data = self.get(
            "/api/v4/search",
            params={"scope": "merge_requests", "search": query, "per_page": per_page},
        )
        return data if isinstance(data, list) else []

    def list_merge_requests(
        self,
        *,
        search: Optional[str] = None,
        state: str = "all",
        per_page: int = 50,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": state, "per_page": per_page, "order_by": "updated_at"}
        if search:
            params["search"] = search
        data = self.get("/api/v4/merge_requests", params=params)
        return data if isinstance(data, list) else []

    def paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = 100,
        max_pages: int = 20,
    ) -> Iterator[Dict[str, Any]]:
        params = dict(params or {})
        page = 1
        while page <= max_pages:
            page_params = {**params, "page": page, "per_page": per_page}
            data = self.get(path, params=page_params)
            if not isinstance(data, list) or not data:
                break
            for item in data:
                yield item
            if len(data) < per_page:
                break
            page += 1


class GitHubClient(_BaseGitClient):
    """GitHub REST API client (pull requests)."""

    def _configure_auth(self, pat: str) -> None:
        self.session.headers["Authorization"] = f"Bearer {pat}"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"

    def get_pull_request(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        return self.get(f"/repos/{owner}/{repo}/pulls/{number}")

    def get_pull_commits(
        self, owner: str, repo: str, number: int
    ) -> List[Dict[str, Any]]:
        data = self.get(f"/repos/{owner}/{repo}/pulls/{number}/commits", params={"per_page": 100})
        return data if isinstance(data, list) else []

    def get_pull_files(
        self, owner: str, repo: str, number: int
    ) -> List[Dict[str, Any]]:
        data = self.get(f"/repos/{owner}/{repo}/pulls/{number}/files", params={"per_page": 100})
        return data if isinstance(data, list) else []

    def get_pull_reviews(
        self, owner: str, repo: str, number: int
    ) -> List[Dict[str, Any]]:
        data = self.get(f"/repos/{owner}/{repo}/pulls/{number}/reviews", params={"per_page": 100})
        return data if isinstance(data, list) else []

    def get_pull_review_comments(
        self, owner: str, repo: str, number: int
    ) -> List[Dict[str, Any]]:
        data = self.get(
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            params={"per_page": 100},
        )
        return data if isinstance(data, list) else []

    def get_issue_comments(
        self, owner: str, repo: str, number: int
    ) -> List[Dict[str, Any]]:
        data = self.get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": 100},
        )
        return data if isinstance(data, list) else []

    def search_pull_requests(self, query: str, per_page: int = 20) -> List[Dict[str, Any]]:
        data = self.get(
            "/search/issues",
            params={"q": f"{query} is:pr", "per_page": per_page},
        )
        return (data or {}).get("items") or []


def create_git_client(config: GitConfig) -> _BaseGitClient:
    if config.provider == "github":
        return GitHubClient(config)
    return GitLabClient(config)
