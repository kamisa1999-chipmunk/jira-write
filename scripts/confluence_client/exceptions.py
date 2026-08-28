"""Exceptions for the Confluence client layer."""


class ConfluenceError(Exception):
    """Base error for Confluence client failures."""


class ConfluenceAuthError(ConfluenceError):
    """Authentication or authorization failure (401/403)."""


class ConfluenceApiError(ConfluenceError):
    """Non-success HTTP response from Confluence."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class ConfluenceConfigError(ConfluenceError):
    """Missing or invalid Confluence configuration."""
