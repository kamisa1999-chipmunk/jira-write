"""Exceptions for the Git (GitLab/GitHub) client layer."""


class GitError(Exception):
    """Base error for Git client failures."""


class GitAuthError(GitError):
    """Authentication or authorization failure (401/403)."""


class GitApiError(GitError):
    """Non-success HTTP response from Git API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class GitConfigError(GitError):
    """Missing or invalid Git configuration."""
