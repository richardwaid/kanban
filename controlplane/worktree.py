"""Git worktree management for isolated worker execution."""

from __future__ import annotations

import logging
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
