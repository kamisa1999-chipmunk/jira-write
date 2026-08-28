"""Generic Confluence REST client: auth, GET/POST, safe logging."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from .config import ConfluenceConfig
from .exceptions import ConfluenceApiError, ConfluenceAuthError

logger = logging.getLogger(__name__)


class ConfluenceClient:
    """Low-level HTTP client. Knows nothing about sprint page templates."""

    def __init__(self, config: ConfluenceConfig) -> None:
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

    def __enter__(self) -> "ConfluenceClient":
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
        logger.debug("Confluence %s %s params=%s", method.upper(), path, params)

        response = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise ConfluenceAuthError(
                "Confluence вернула 401: проверь CONFLUENCE_PAT / логин"
            )
        if response.status_code == 403:
            raise ConfluenceAuthError(
                "Confluence вернула 403: нет прав на этот ресурс"
            )
        if not response.ok:
            raise ConfluenceApiError(
                response.status_code,
                f"Confluence API error {response.status_code}: "
                f"{response.text[:500]}",
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
