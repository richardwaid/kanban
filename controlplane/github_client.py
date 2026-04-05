"""GitHub App client for the kanban agent system.

Handles GitHub App JWT authentication, installation token lifecycle,
and GitHub API operations (issues, PRs, comments, labels).
"""

from __future__ import annotations

import logging
import os
import time

import httpx
import jwt

logger = logging.getLogger(__name__)


class GitHubAppClient:
    """GitHub App API client with auto-refreshing installation tokens."""

    API_BASE = "https://api.github.com"

    def __init__(self, app_id: str, private_key_path: str,
                 installation_id: str, repo: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.repo = repo  # "owner/repo"

        with open(private_key_path) as f:
            self._private_key = f.read()

        self._token: str | None = None
        self._token_expires: float = 0
        self._http = httpx.Client(timeout=30)

        logger.info("GitHub App client initialized for %s (app=%s, installation=%s)",
                     repo, app_id, installation_id)

    # --- Token lifecycle ---

    def _make_jwt(self) -> str:
        """Sign a short-lived JWT for the GitHub App."""
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": self.app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _refresh_token(self) -> None:
        """Exchange the app JWT for an installation access token."""
        app_jwt = self._make_jwt()
        resp = self._http.post(
            f"{self.API_BASE}/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}",
                     "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        # Tokens last 1 hour; refresh with 5 min buffer
        self._token_expires = time.time() + 3300
        logger.debug("GitHub installation token refreshed (expires in ~55min)")

    def get_token(self) -> str:
        """Return a valid installation token, refreshing if needed."""
        if not self._token or time.time() > self._token_expires:
            self._refresh_token()
        return self._token

    # --- HTTP helpers ---

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept": "application/vnd.github+json",
        }

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.API_BASE}{path}" if path.startswith("/") else path
        resp = self._http.request(method, url, headers=self._headers(), **kwargs)
        resp.raise_for_status()
        return resp

    # --- Issues ---

    def fetch_issues(self, labels: list[str] | None = None,
                     state: str = "open") -> list[dict]:
        params = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)
        resp = self._request("GET", f"/repos/{self.repo}/issues", params=params)
        # Filter out pull requests (GitHub API returns PRs in issues endpoint)
        return [i for i in resp.json() if "pull_request" not in i]

    def add_comment(self, issue_number: int, body: str) -> dict:
        resp = self._request(
            "POST", f"/repos/{self.repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return resp.json()

    def update_labels(self, issue_number: int,
                      add: list[str] | None = None,
                      remove: list[str] | None = None) -> None:
        if add:
            self._request(
                "POST", f"/repos/{self.repo}/issues/{issue_number}/labels",
                json={"labels": add},
            )
        if remove:
            for label in remove:
                try:
                    self._request(
                        "DELETE",
                        f"/repos/{self.repo}/issues/{issue_number}/labels/{label}",
                    )
                except httpx.HTTPStatusError:
                    pass  # Label might not exist

    def close_issue(self, issue_number: int) -> None:
        self._request(
            "PATCH", f"/repos/{self.repo}/issues/{issue_number}",
            json={"state": "closed"},
        )

    # --- Pull Requests ---

    def create_pr(self, head_branch: str, title: str, body: str,
                  base: str | None = None) -> dict:
        if base is None:
            # Detect default branch from repo
            repo_resp = self._request("GET", f"/repos/{self.repo}")
            base = repo_resp.json().get("default_branch", "main")
        resp = self._request(
            "POST", f"/repos/{self.repo}/pulls",
            json={"title": title, "body": body, "head": head_branch, "base": base},
        )
        return resp.json()

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> dict:
        resp = self._request(
            "PUT", f"/repos/{self.repo}/pulls/{pr_number}/merge",
            json={"merge_method": merge_method},
        )
        return resp.json()

    def get_pr(self, pr_number: int) -> dict:
        resp = self._request("GET", f"/repos/{self.repo}/pulls/{pr_number}")
        return resp.json()

    def create_pr_review(self, pr_number: int, body: str,
                         event: str = "COMMENT") -> dict:
        """Create a PR review. event: APPROVE, REQUEST_CHANGES, or COMMENT."""
        resp = self._request(
            "POST", f"/repos/{self.repo}/pulls/{pr_number}/reviews",
            json={"body": body, "event": event},
        )
        return resp.json()


def create_client_from_env() -> GitHubAppClient | None:
    """Create a GitHubAppClient from environment variables. Returns None if not configured."""
    app_id = os.environ.get("KANBAN_GITHUB_APP_ID")
    key_path = os.environ.get("KANBAN_GITHUB_APP_PRIVATE_KEY_PATH")
    install_id = os.environ.get("KANBAN_GITHUB_APP_INSTALLATION_ID")
    repo = os.environ.get("KANBAN_GITHUB_REPO")

    if not all([app_id, key_path, install_id, repo]):
        return None

    return GitHubAppClient(app_id, key_path, install_id, repo)
