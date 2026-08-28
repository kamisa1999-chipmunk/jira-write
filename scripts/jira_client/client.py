"""Generic Jira REST client: auth, GET/POST, pagination, safe logging."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

import requests

from .config import JiraConfig
from .exceptions import JiraApiError, JiraAuthError

logger = logging.getLogger(__name__)


class JiraClient:
    """Low-level HTTP client. Knows nothing about projects, sprints, or reports."""

    def __init__(self, config: JiraConfig) -> None:
        self.base_url = config.url
        self.timeout = config.timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

        if config.pat:
            self.session.headers["Authorization"] = f"Bearer {config.pat}"
        else:
            self.session.auth = (config.username, config.password)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "JiraClient":
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
        url = f"{self.base_url}{path}"
        # Never log Authorization / cookies / body with credentials
        logger.debug("Jira %s %s params=%s", method.upper(), path, params)

        response = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise JiraAuthError(
                "Jira вернула 401: проверь токен/логин и права доступа к проекту"
            )
        if response.status_code == 403:
            raise JiraAuthError("Jira вернула 403: нет прав на этот ресурс")
        if not response.ok:
            raise JiraApiError(
                response.status_code,
                f"Jira API error {response.status_code}: {response.text[:500]}",
            )

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._request("POST", path, params=params, json_body=json_body)

    def put(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._request("PUT", path, params=params, json_body=json_body)

    def delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._request("DELETE", path, params=params, json_body=json_body)

    def paginate(
        self,
        path: str,
        *,
        results_key: str = "values",
        start_at: int = 0,
        max_results: int = 50,
        params: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield items from Agile/API pages (`values` or `issues`)."""
        params = dict(params or {})
        current = start_at

        while True:
            page_params = {
                **params,
                "startAt": current,
                "maxResults": max_results,
            }
            data = self.get(path, params=page_params)
            batch: List[Dict[str, Any]] = data.get(results_key, [])
            for item in batch:
                yield item

            total = data.get("total")
            is_last = data.get("isLast")
            if is_last is True:
                break
            if not batch:
                break
            if total is not None and current + len(batch) >= total:
                break
            if total is None and len(batch) < max_results:
                break

            current += len(batch)

    def get_server_info(self) -> Dict[str, Any]:
        return self.get("/rest/api/2/serverInfo")
