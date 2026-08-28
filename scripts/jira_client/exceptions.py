"""Exceptions for the Jira client layer."""


class JiraError(Exception):
    """Base error for Jira client failures."""


class JiraAuthError(JiraError):
    """Authentication or authorization failure (401/403)."""


class JiraApiError(JiraError):
    """Non-success HTTP response from Jira."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class JiraConfigError(JiraError):
    """Missing or invalid configuration."""
