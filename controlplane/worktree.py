"""Git worktree management for isolated worker execution and GitHub repo setup."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache the detected default branch per repo path
_default_branch_cache: dict[str, str] = {}


def _run_git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the given repo."""
    cmd = ["git", "-C", repo_path, *args]
    logger.debug("git: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def detect_default_branch(repo_path: str) -> str:
    """Detect the default branch (master, main, etc.) via HEAD."""
    if repo_path in _default_branch_cache:
        return _default_branch_cache[repo_path]
    result = _run_git(repo_path, "symbolic-ref", "--short", "HEAD")
    branch = result.stdout.strip()
    if not branch:
        branch = "master"
    _default_branch_cache[repo_path] = branch
    logger.info("Detected default branch: %s", branch)
    return branch


def get_worktree_path(repo_path: str, key: str) -> str:
    """Compute the deterministic worktree path for a given key (task or feature ID)."""
    return str(Path(repo_path).parent / ".worktrees" / key)


def ensure_feature_worktree(repo_path: str, feature_id: str) -> tuple[str, str]:
    """Create or reuse a persistent worktree for a feature/bug.

    Returns (worktree_path, branch_name).
    The worktree persists across the feature lifecycle — workers, continuations,
    and follow-up iterations all share it.
    """
    wt_path = get_worktree_path(repo_path, feature_id)
    branch_name = f"work/{feature_id}"

    if Path(wt_path).exists():
        # Worktree already exists — reuse it
        logger.info("Reusing worktree for %s at %s", feature_id, wt_path)
        setup_memory_symlink(repo_path, wt_path)
        return wt_path, branch_name

    # Determine base: default branch
    base_ref = detect_default_branch(repo_path)

    # Delete stale branch if it exists without a worktree
    existing = _run_git(repo_path, "branch", "--list", branch_name, check=False)
    if existing.stdout.strip():
        logger.warning("Stale branch %s exists without worktree, deleting.", branch_name)
        _run_git(repo_path, "branch", "-D", branch_name, check=False)

    Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
    _run_git(repo_path, "worktree", "add", "-b", branch_name, wt_path, base_ref)
    setup_memory_symlink(repo_path, wt_path)
    logger.info("Created feature worktree for %s at %s (branch: %s)", feature_id, wt_path, branch_name)
    return wt_path, branch_name


def create_worktree(repo_path: str, task_id: str, base_ref: str | None = None) -> str:
    """Create a temporary worktree for a task (e.g. freebase merge).

    Returns the worktree path. For persistent feature worktrees, use ensure_feature_worktree.
    """
    if base_ref is None:
        base_ref = detect_default_branch(repo_path)

    wt_path = get_worktree_path(repo_path, task_id)
    branch_name = f"task/{task_id}"

    # Clean up if a stale worktree exists at this path
    if Path(wt_path).exists():
        logger.warning("Stale worktree at %s, removing first.", wt_path)
        remove_worktree(repo_path, task_id)

    # Delete stale branch if it exists (e.g. from a previous failed attempt)
    existing = _run_git(repo_path, "branch", "--list", branch_name, check=False)
    if existing.stdout.strip():
        logger.warning("Stale branch %s exists, deleting.", branch_name)
        _run_git(repo_path, "branch", "-D", branch_name, check=False)

    # Ensure parent directory exists
    Path(wt_path).parent.mkdir(parents=True, exist_ok=True)

    _run_git(repo_path, "worktree", "add", "-b", branch_name, wt_path, base_ref)
    logger.info("Created worktree for %s at %s (branch: %s, base: %s)",
                task_id, wt_path, branch_name, base_ref)
    return wt_path


def remove_worktree(repo_path: str, key: str) -> None:
    """Remove the worktree directory but keep the branch (commits are still needed)."""
    wt_path = get_worktree_path(repo_path, key)

    # Remove the worktree directory via git
    result = _run_git(repo_path, "worktree", "remove", "--force", wt_path, check=False)
    if result.returncode != 0:
        logger.warning("git worktree remove failed for %s: %s", key, result.stderr.strip())
        # Fall back to manual directory removal
        if Path(wt_path).exists():
            shutil.rmtree(wt_path, ignore_errors=True)
            logger.info("Manually removed worktree dir %s", wt_path)

    # Prune stale worktree entries
    _run_git(repo_path, "worktree", "prune", check=False)
    # NOTE: branch is intentionally kept — it's needed for review and merge.
    # Call delete_branch() explicitly after the branch has been merged.


def remove_feature_worktree(repo_path: str, feature_id: str) -> None:
    """Remove a feature's persistent worktree AND its branch. Call on done/abandon."""
    remove_worktree(repo_path, feature_id)
    branch_name = f"work/{feature_id}"
    delete_branch(repo_path, branch_name)
    logger.info("Cleaned up feature worktree and branch for %s", feature_id)


def delete_branch(repo_path: str, branch_name: str) -> None:
    """Delete a task branch after it has been merged or is no longer needed."""
    result = _run_git(repo_path, "branch", "-D", branch_name, check=False)
    if result.returncode == 0:
        logger.debug("Deleted branch %s", branch_name)
    else:
        logger.debug("Branch %s already deleted or not found", branch_name)


def merge_branch(repo_path: str, branch_name: str, target: str | None = None) -> str:
    """Squash-merge a task branch into the target branch.

    Returns the resulting commit hash. Raises on merge failure.
    """
    if target is None:
        target = detect_default_branch(repo_path)

    # Ensure we're on the target branch in the main worktree
    _run_git(repo_path, "checkout", target)

    # Squash merge
    result = _run_git(repo_path, "merge", "--squash", branch_name, check=False)
    if result.returncode != 0:
        # Reset any partial merge state
        _run_git(repo_path, "reset", "--hard", "HEAD", check=False)
        raise RuntimeError(
            f"Merge of {branch_name} into {target} failed: {result.stderr.strip()}"
        )

    # Commit the squash
    commit_result = _run_git(
        repo_path, "commit", "--no-edit",
        "-m", f"Merge {branch_name} (squash)",
        check=False,
    )
    if commit_result.returncode != 0:
        # Could be "nothing to commit" if already merged
        logger.warning("Squash commit returned %d: %s",
                       commit_result.returncode, commit_result.stderr.strip())

    # Get the resulting commit hash
    hash_result = _run_git(repo_path, "rev-parse", "HEAD")
    commit_hash = hash_result.stdout.strip()
    logger.info("Merged %s into %s: %s", branch_name, target, commit_hash[:12])
    return commit_hash


# --- GitHub repo setup ---

def setup_repo(repo_path: str, github_client) -> None:
    """Ensure the local repo is set up for GitHub operations.

    Clones if missing, configures remote URL with fresh token, sets bot author.
    """
    repo = Path(repo_path)
    token = github_client.get_token()
    remote_url = f"https://x-access-token:{token}@github.com/{github_client.repo}.git"

    if not repo.exists() or not (repo / ".git").exists():
        # Clone the repo
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", remote_url, str(repo)],
            capture_output=True, text=True, check=True,
        )
        logger.info("Cloned %s into %s", github_client.repo, repo_path)
    else:
        # Update remote URL with fresh token
        remotes = _run_git(repo_path, "remote", check=False).stdout.strip()
        if "origin" in remotes.splitlines():
            _run_git(repo_path, "remote", "set-url", "origin", remote_url, check=False)
        else:
            _run_git(repo_path, "remote", "add", "origin", remote_url, check=False)
        logger.info("Updated remote URL for %s", github_client.repo)

    # Set bot author identity
    app_id = github_client.app_id
    _run_git(repo_path, "config", "user.name", "kanban-agents[bot]", check=False)
    _run_git(repo_path, "config", "user.email",
             f"{app_id}+kanban-agents[bot]@users.noreply.github.com", check=False)


def refresh_remote_token(repo_path: str, github_client) -> None:
    """Update the remote URL with a fresh installation token."""
    token = github_client.get_token()
    remote_url = f"https://x-access-token:{token}@github.com/{github_client.repo}.git"
    _run_git(repo_path, "remote", "set-url", "origin", remote_url, check=False)


def push_branch(repo_path: str, branch_name: str, github_client) -> None:
    """Push a branch to the GitHub remote."""
    refresh_remote_token(repo_path, github_client)
    result = _run_git(repo_path, "push", "-u", "origin", branch_name, check=False)
    if result.returncode != 0:
        # Force push if branch diverged (e.g. after rebase)
        logger.warning("Push failed, trying force push: %s", result.stderr.strip())
        _run_git(repo_path, "push", "--force-with-lease", "-u", "origin", branch_name)
    logger.info("Pushed branch %s to origin", branch_name)


def sync_default_branch(repo_path: str, github_client=None) -> None:
    """Sync local default branch with remote after a PR merge."""
    if github_client:
        refresh_remote_token(repo_path, github_client)
    default = detect_default_branch(repo_path)
    _run_git(repo_path, "fetch", "origin", check=False)
    _run_git(repo_path, "checkout", default, check=False)
    _run_git(repo_path, "reset", "--hard", f"origin/{default}", check=False)
    logger.info("Synced local %s with origin", default)


# --- Memory symlinks ---

def _claude_project_slug(path: str) -> str:
    """Convert an absolute path to Claude CLI's project directory slug."""
    return path.replace("/", "-").lstrip("-")


def setup_memory_symlink(repo_path: str, worktree_path: str) -> None:
    """Symlink a worktree's Claude memory directory to the repo root's memory.

    This gives all agents shared learning across features while keeping
    conversation sessions isolated per worktree.
    """
    claude_dir = Path.home() / ".claude" / "projects"
    repo_slug = _claude_project_slug(str(Path(repo_path).resolve()))
    wt_slug = _claude_project_slug(str(Path(worktree_path).resolve()))

    repo_memory = claude_dir / repo_slug / "memory"
    wt_project = claude_dir / wt_slug
    wt_memory = wt_project / "memory"

    # Ensure repo memory dir exists
    repo_memory.mkdir(parents=True, exist_ok=True)
    # Ensure worktree project dir exists
    wt_project.mkdir(parents=True, exist_ok=True)

    # Create symlink if it doesn't exist (or is not already a symlink)
    if wt_memory.is_symlink():
        if wt_memory.resolve() == repo_memory.resolve():
            return  # Already correct
        wt_memory.unlink()
    elif wt_memory.exists():
        # Real directory — move contents to repo memory, then replace with symlink
        for item in wt_memory.iterdir():
            target = repo_memory / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(wt_memory)

    wt_memory.symlink_to(repo_memory)
    logger.info("Symlinked memory: %s -> %s", wt_slug, repo_slug)


def cleanup_stale_worktrees(repo_path: str) -> None:
    """Prune stale git worktrees and remove orphaned worktree directories."""
    _run_git(repo_path, "worktree", "prune", check=False)

    worktrees_dir = Path(repo_path).parent / ".worktrees"
    if worktrees_dir.exists():
        # List currently valid worktrees
        result = _run_git(repo_path, "worktree", "list", "--porcelain", check=False)
        valid_paths = set()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                valid_paths.add(line.split(" ", 1)[1])

        # Remove dirs that aren't valid worktrees
        for child in worktrees_dir.iterdir():
            if child.is_dir() and str(child) not in valid_paths:
                logger.info("Removing orphaned worktree dir: %s", child)
                shutil.rmtree(child, ignore_errors=True)
