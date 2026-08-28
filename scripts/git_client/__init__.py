"""Shared GitLab/GitHub REST client (config, HTTP, errors)."""

from .client import GitHubClient, GitLabClient, create_git_client
from .config import GitConfig, load_git_config
from .exceptions import GitApiError, GitAuthError, GitConfigError, GitError

__all__ = [
    "GitApiError",
    "GitAuthError",
    "GitConfig",
    "GitConfigError",
    "GitError",
    "GitHubClient",
    "GitLabClient",
    "create_git_client",
    "load_git_config",
]
