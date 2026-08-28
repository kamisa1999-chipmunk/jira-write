"""Shared Confluence REST client (config, HTTP, errors)."""

from .client import ConfluenceClient
from .config import ConfluenceConfig, load_confluence_config
from .exceptions import (
    ConfluenceApiError,
    ConfluenceAuthError,
    ConfluenceConfigError,
    ConfluenceError,
)

__all__ = [
    "ConfluenceClient",
    "ConfluenceConfig",
    "ConfluenceApiError",
    "ConfluenceAuthError",
    "ConfluenceConfigError",
    "ConfluenceError",
    "load_confluence_config",
]
