"""Supervisor loop: polls for ready tasks, dispatches to Claude CLI agents.

Includes a watchdog that detects and kills stuck tasks.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from controlplane.models import (
    FeatureStatus,
    FreebaseResult,
    ReviewOutcome,
    Task,
    TriageVerdict,
    _now,
)
from controlplane.runner import run_freebase, run_triage, run_planner, run_code_worker, run_code_reviewer, kill_task, is_task_alive
from controlplane import store, worktree

logger = logging.getLogger(__name__)

REPO_PATH = os.environ.get("KANBAN_REPO_PATH", str(Path(__file__).parent.parent / "repo"))
POLL_INTERVAL = int(os.environ.get("KANBAN_POLL_INTERVAL", "5"))
STUCK_TIMEOUT = int(os.environ.get("KANBAN_STUCK_TIMEOUT", "300"))  # 5 minutes
MAX_CONCURRENT = int(os.environ.get("KANBAN_MAX_CONCURRENT", "5"))
MAX_CONCURRENT_WORKERS = int(os.environ.get("KANBAN_MAX_CONCURRENT_WORKERS", "3"))
MAX_CONCURRENT_FREEBASE = 1  # Merges must be serialized to avoid race conditions

# Dedicated thread pool for task execution — separate from the default executor
# so that long-running tasks don't starve the watchdog / polling.
_task_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="agent")

# Track running tasks: task_id -> (asyncio.Future, agent_type)
_running: dict[str, tuple[asyncio.Future, str]] = {}


def _load_feature_or_bail(task: Task):
    """Load the feature for a task. Returns None and logs error if not found."""
    feature = store.load_feature(task.root_feature_id)
    if not feature:
        logger.error("Feature %s not found for task %s", task.root_feature_id, task.id)
    return feature


def _handle_triage(task: Task) -> None:
    """Execute a triage task and decide the next step for the bug report."""
    feature = _load_feature_or_bail(task)
    if not feature:
        return

    logger.info("Running triage for %s (feature: %s)", task.id, feature.id)

    result = run_triage(task, feature, REPO_PATH)

    # Save triage artifact
    store.save_triage_result(task.id, asdict(result))

    # Mark triage task done
    store.move_task(task, "done")
    logger.info("Triage %s done. Verdict: %s", task.id, result.verdict)

    if result.verdict == TriageVerdict.VALID.value:
        # Valid bug — create code_worker task directly (no planning step)
        worker_id = store._next_task_id()
        worker_task = Task(
            id=worker_id,
            root_feature_id=feature.id,
            agent="code_worker",
            type="implement",
            title=feature.title,
            description=f"{feature.description}\n\nTriage summary: {result.summary}",
            priority=task.priority,
            parent_task_id=task.id,
        )
        store.save_task(worker_task)

        feature.status = FeatureStatus.IN_PROGRESS.value
        feature.current_task_id = worker_id
        store.save_feature(feature)
        logger.info("Bug %s triaged as valid. Created worker task %s", feature.id, worker_id)

    elif result.verdict == TriageVerdict.NEEDS_INFO.value:
        # Needs more info — park the feature for user
        feature.status = FeatureStatus.NEEDS_INFO.value
        feature.current_task_id = None
        store.save_feature(feature)
        logger.info("Bug %s needs more info. %d question(s).", feature.id, len(result.questions))


def _handle_planner(task: Task) -> None:
    """Execute a planner task and move the feature to awaiting_approval."""
    feature = _load_feature_or_bail(task)
    if not feature:
        return

    is_revision = getattr(task, 'is_continuation', False)
    logger.info("Running planner for %s (feature: %s%s)",
                task.id, feature.id, ", revision" if is_revision else "")

    # Check for revision context (previous plan + feedback stored as artifact)
    previous_plan = ""
    feedback = ""
    if is_revision:
        revision_artifact = store.load_artifact("planner_results", task.id)
        if revision_artifact:
            previous_plan = revision_artifact.get("plan", "")
            feedback = revision_artifact.get("feedback", "")

    result = run_planner(task, feature, REPO_PATH,
                         previous_plan=previous_plan, feedback=feedback,
                         resume=is_revision)

    # Save planner artifact
    store.save_planner_result(task.id, asdict(result))

    # Mark planner task done
    store.move_task(task, "done")
    logger.info("Planner %s done.", task.id)

    # Move feature to awaiting_approval
    feature.status = FeatureStatus.AWAITING_APPROVAL.value
    feature.current_task_id = None
    store.save_feature(feature)
    logger.info("Feature %s is now awaiting approval.", feature.id)


def _has_outstanding_human_tasks(feature_id: str) -> bool:
    """Check if a feature has any unresolved human/blocked tasks."""
    for t in store.list_tasks(status="blocked"):
        if t.root_feature_id == feature_id:
            return True
    return False


def _mark_feature_done(feature: Feature) -> None:
    """Mark a feature done, unless it has outstanding human tasks."""
    if _has_outstanding_human_tasks(feature.id):
        logger.info("Feature %s has outstanding human tasks — keeping in_progress.", feature.id)
        feature.status = FeatureStatus.IN_PROGRESS.value
        feature.current_task_id = None
        store.save_feature(feature)
        return
    feature.status = FeatureStatus.DONE.value
    feature.current_task_id = None
    store.save_feature(feature)
    # Clean up the persistent feature worktree
    worktree.remove_feature_worktree(REPO_PATH, feature.id)
    logger.info("Feature %s marked done.", feature.id)


def _handle_freebase(task: Task) -> None:
    """Execute a freebase (merge master) task — merge approved branch into default.

    Strategy:
    1. Try a programmatic rebase + ff merge (fast, no agent needed).
    2. If rebase has conflicts, invoke the freebase Claude agent to resolve them.
    """
    feature = _load_feature_or_bail(task)
    if not feature:
        return

    branch_name = task.branch_name
    if not branch_name:
        logger.error("Freebase task %s has no branch_name", task.id)
        store.move_task(task, "failed")
        return

    default_branch = worktree.detect_default_branch(REPO_PATH)
    logger.info("Freebase %s: merging %s into %s for %s",
                task.id, branch_name, default_branch, feature.id)

    # Step 1: Try programmatic rebase in the feature worktree (no agent).
    # We must rebase inside the worktree because the branch is checked out there —
    # git won't let us rebase it from the main checkout.
    feat_wt = worktree.get_worktree_path(REPO_PATH, feature.id)
    has_worktree = Path(feat_wt).exists()

    if has_worktree:
        # Fetch latest default branch into the worktree, then rebase onto it
        worktree._run_git(feat_wt, "fetch", ".", f"{default_branch}:{default_branch}", check=False)
        rebase_result = worktree._run_git(feat_wt, "rebase", default_branch, check=False)
    else:
        rebase_result = worktree._run_git(
            REPO_PATH, "rebase", default_branch, branch_name, check=False,
        )

    if rebase_result.returncode == 0:
        # Rebase clean — fast-forward the default branch from the main checkout
        worktree._run_git(REPO_PATH, "checkout", default_branch)
        ff_result = worktree._run_git(
            REPO_PATH, "merge", "--ff-only", branch_name, check=False,
        )
        if ff_result.returncode == 0:
            head = worktree._run_git(REPO_PATH, "rev-parse", "HEAD").stdout.strip()
            store.save_worker_result(task.id, asdict(FreebaseResult(
                task_id=task.id, status="done", merge_commit_id=head,
                summary=f"Clean rebase + ff merge of {branch_name}",
            )))
            store.move_task(task, "done")

            feature.latest_commit_id = head
            store.save_feature(feature)
            worktree.delete_branch(REPO_PATH, branch_name)
            _mark_feature_done(feature)
            return

        # ff failed — fall back to merge commit
        logger.info("ff-only failed after rebase, falling back to merge commit for %s", branch_name)
        merge_result = worktree._run_git(
            REPO_PATH, "merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}", check=False,
        )
        if merge_result.returncode == 0:
            head = worktree._run_git(REPO_PATH, "rev-parse", "HEAD").stdout.strip()
            store.save_worker_result(task.id, asdict(FreebaseResult(
                task_id=task.id, status="done", merge_commit_id=head,
                summary=f"Rebase + merge commit of {branch_name}",
            )))
            store.move_task(task, "done")

            feature.latest_commit_id = head
            store.save_feature(feature)
            worktree.delete_branch(REPO_PATH, branch_name)
            _mark_feature_done(feature)
            return

        worktree._run_git(REPO_PATH, "merge", "--abort", check=False)
        logger.error("Both ff-only and merge failed for %s", branch_name)

    else:
        # Rebase had conflicts — abort it
        abort_path = feat_wt if has_worktree else REPO_PATH
        worktree._run_git(abort_path, "rebase", "--abort", check=False)
        logger.info("Rebase of %s had conflicts, invoking freebase agent.", branch_name)

    # Step 2: Conflicts — invoke the freebase Claude agent in a worktree
    wt_path = worktree.create_worktree(REPO_PATH, task.id, branch_name)

    try:
        result = run_freebase(task, feature, wt_path, branch_name, default_branch)
        store.save_worker_result(task.id, asdict(result))
        store.move_task(task, "done")
        logger.info("Freebase agent %s done. Status: %s", task.id, result.status)

        if result.status == "done" and result.merge_commit_id:
            # Agent resolved conflicts. Merge the branch into main.
            worktree._run_git(REPO_PATH, "checkout", default_branch)

            # Try ff-only first; fall back to regular merge if branches diverged
            ff_result = worktree._run_git(
                REPO_PATH, "merge", "--ff-only", branch_name, check=False,
            )
            if ff_result.returncode != 0:
                logger.info("ff-only failed, falling back to merge commit for %s", branch_name)
                merge_result = worktree._run_git(
                    REPO_PATH, "merge", "--no-ff", branch_name,
                    "-m", f"Merge {branch_name}", check=False,
                )
                if merge_result.returncode != 0:
                    logger.error("Merge also failed: %s", merge_result.stderr.strip())
                    worktree._run_git(REPO_PATH, "merge", "--abort", check=False)
                    feature.status = FeatureStatus.IN_PROGRESS.value
                    feature.current_task_id = None
                    store.save_feature(feature)
                    return

            head = worktree._run_git(REPO_PATH, "rev-parse", "HEAD").stdout.strip()
            feature.latest_commit_id = head
            store.save_feature(feature)
            worktree.delete_branch(REPO_PATH, branch_name)
            _mark_feature_done(feature)

        elif result.status == "conflict":
            # Agent couldn't resolve — human_tasks will be created by _process_task
            logger.warning("Freebase %s: unresolvable conflict for %s", task.id, feature.id)
            feature.status = FeatureStatus.IN_PROGRESS.value
            feature.current_task_id = None
            store.save_feature(feature)

    finally:
        worktree.remove_worktree(REPO_PATH, task.id)


def _handle_code_worker(task: Task) -> None:
    """Execute a code_worker task in a persistent per-feature worktree."""
    feature = _load_feature_or_bail(task)
    if not feature:
        return

    # Use persistent per-feature worktree — all workers for the same feature
    # share the worktree AND the conversation history via --continue.
    wt_path, branch_name = worktree.ensure_feature_worktree(REPO_PATH, feature.id)

    # Always pass --continue. If a prior conversation exists in the worktree,
    # Claude picks it up (retries, follow-ups, human Q&A, reviewer iterations
    # all share context). If none exists (first ever run), it's a no-op.
    logger.info("Running code_worker for %s (feature: %s, iteration: %d)",
                task.id, feature.id, task.iteration)

    # Import janky_mode from server (avoids circular import at module level)
    from controlplane.server import _janky_mode
    result = run_code_worker(task, feature, wt_path, janky_mode=_janky_mode, resume=True)

    # Save branch name on the task
    task.branch_name = branch_name
    store.save_task(task)

    # Save worker artifact
    store.save_worker_result(task.id, asdict(result))

    # Mark worker task done
    store.move_task(task, "done")
    logger.info("Worker %s done. Commit: %s (branch: %s)", task.id, result.commit_id, branch_name)

    # Update feature
    feature.latest_commit_id = result.commit_id
    feature.iteration_count = task.iteration

    # Create reviewer task
    reviewer_id = store._next_task_id()
    reviewer_task = Task(
        id=reviewer_id,
        root_feature_id=feature.id,
        agent="code_reviewer",
        type="review",
        title=f"Review {task.id}",
        description=f"Review commit {result.commit_id} for feature: {feature.title}",
        priority=task.priority,
        iteration=task.iteration,
        parent_task_id=task.id,
        review_commit_id=result.commit_id,
    )
    store.save_task(reviewer_task)

    feature.current_task_id = reviewer_id
    store.save_feature(feature)
    logger.info("Created reviewer task %s for commit %s", reviewer_id, result.commit_id)


def _handle_code_reviewer(task: Task) -> None:
    """Execute a code_reviewer task in the feature worktree (where the commit lives)."""
    feature = _load_feature_or_bail(task)
    if not feature:
        return

    # Run reviewer in the feature worktree so it can see the worker's commits
    # on the feature branch (not just on master).
    wt_path, branch_name = worktree.ensure_feature_worktree(REPO_PATH, feature.id)

    # Pre-review rebase: bring the feature branch up to date with the default branch.
    # This reduces merge conflicts at merge time and lets the reviewer see the code
    # in the context of the latest main. If the rebase conflicts, abort and let the
    # reviewer run anyway — it can flag the issue.
    default_branch = worktree.detect_default_branch(REPO_PATH)
    rebase_result = worktree._run_git(wt_path, "rebase", default_branch, check=False)
    if rebase_result.returncode != 0:
        worktree._run_git(wt_path, "rebase", "--abort", check=False)
        logger.warning("Pre-review rebase of %s onto %s failed (conflicts). Reviewer will run on stale base.",
                        branch_name, default_branch)
    else:
        logger.info("Pre-review rebase of %s onto %s succeeded.", branch_name, default_branch)

    # Find the approved plan for this feature so the reviewer can check plan compliance
    approved_plan = ""
    for t in store.list_tasks(status="done"):
        if t.root_feature_id == feature.id and t.agent == "planner":
            plan_artifact = store.load_artifact("planner_results", t.id)
            if plan_artifact and plan_artifact.get("plan"):
                approved_plan = plan_artifact["plan"]

    logger.info("Running code_reviewer for %s (feature: %s, commit: %s, worktree: %s)",
                task.id, feature.id, task.review_commit_id, wt_path)

    result = run_code_reviewer(task, feature, wt_path, approved_plan=approved_plan)

    # Save review artifact
    store.save_review_result(task.id, asdict(result))

    # Mark reviewer task done
    store.move_task(task, "done")
    logger.info("Reviewer %s done. Outcome: %s", task.id, result.review_outcome)

    # Check for bugs even if approved — bugs always create a worker task
    bugs = [i for i in result.items if i.type == "bug"]
    non_bugs = [i for i in result.items if i.type != "bug"]

    if result.review_outcome == ReviewOutcome.APPROVED.value and not bugs:
        # Clean approval — hand off to freebase for merge
        worker_task = store.load_task(task.parent_task_id) if task.parent_task_id else None
        if worker_task and worker_task.branch_name:
            merge_id = store._next_task_id()
            merge_task = Task(
                id=merge_id,
                root_feature_id=feature.id,
                agent="freebase",
                type="merge",
                title=f"Merge {worker_task.branch_name}",
                description=f"Merge approved branch {worker_task.branch_name} for: {feature.title}",
                priority=task.priority,
                iteration=task.iteration,
                parent_task_id=task.id,
                branch_name=worker_task.branch_name,
            )
            store.save_task(merge_task)

            feature.status = FeatureStatus.MERGING.value
            feature.current_task_id = merge_id
            store.save_feature(feature)
            logger.info("Feature %s approved. Created freebase task %s for %s",
                        feature.id, merge_id, worker_task.branch_name)
        else:
            # No branch to merge (legacy task without worktree) — just mark done
            _mark_feature_done(feature)

    elif result.review_outcome == ReviewOutcome.APPROVED.value and bugs:
        # Approved but bugs found — create worker task for top bug
        top_item = sorted(bugs, key=lambda x: x.priority)[0]
        new_iteration = task.iteration + 1
        logger.info("Feature %s approved but %d bug(s) found; creating fix task", feature.id, len(bugs))

        _create_followup_worker(task, feature, top_item, new_iteration)
        return

    elif result.review_outcome == ReviewOutcome.NEEDS_CHANGES.value:
        # Bugs take priority over improvements
        all_items = sorted(bugs, key=lambda x: x.priority) + sorted(non_bugs, key=lambda x: x.priority)
        if not all_items:
            logger.warning("Reviewer returned needs_changes but no items; treating as approved")
            _mark_feature_done(feature)
            return

        top_item = all_items[0]
        new_iteration = task.iteration + 1

        _create_followup_worker(task, feature, top_item, new_iteration)
        return


def _create_human_tasks(parent_task: Task, feature: Feature, human_tasks: list[dict]) -> None:
    """Create blocked human tasks from an agent's human_tasks output."""
    for ht in human_tasks:
        title = ht.get("title", "Question from agent")
        description = ht.get("description", "")
        tid = store._next_task_id()
        human_task = Task(
            id=tid,
            root_feature_id=feature.id,
            agent="human",
            type="question",
            title=title,
            description=description,
            status="blocked",
            priority=parent_task.priority,
            iteration=parent_task.iteration,
            parent_task_id=parent_task.id,
        )
        store.save_task(human_task)
        logger.info("Created human task %s: %s (from %s)", tid, title, parent_task.id)


def _check_and_create_human_tasks(parent_task: Task, feature: Feature, artifact_task_id: str) -> None:
    """Check saved artifacts for human_tasks and create them if found."""
    for artifact_type in ("triage_results", "planner_results", "worker_results", "review_results"):
        artifact = store.load_artifact(artifact_type, artifact_task_id)
        if artifact and artifact.get("human_tasks"):
            _create_human_tasks(parent_task, feature, artifact["human_tasks"])
            return  # only one artifact per task


def _create_followup_worker(parent_task: Task, feature: Feature, item, new_iteration: int) -> None:
    """Create a follow-up worker task from a review item."""
    worker_id = store._next_task_id()
    worker_task = Task(
        id=worker_id,
        root_feature_id=feature.id,
        agent="code_worker",
        type="implement",
        title=item.title,
        description=item.description,
        priority=parent_task.priority,
        iteration=new_iteration,
        parent_task_id=parent_task.id,
    )
    store.save_task(worker_task)

    feature.current_task_id = worker_id
    feature.iteration_count = new_iteration
    store.save_feature(feature)
    logger.info("Created worker task %s for iteration %d: %s",
                     worker_id, new_iteration, item.title)


# --- Watchdog ---

def _check_stuck_tasks() -> None:
    """Kill tasks that have been in_progress too long or whose process died."""
    in_progress = store.list_tasks(status="in_progress")
    now = datetime.now(timezone.utc)

    for task in in_progress:
        # Skip tasks actively tracked by the supervisor — their handler thread
        # is running even if no Claude CLI subprocess exists (e.g. programmatic merges).
        if task.id in _running:
            continue

        try:
            updated = datetime.fromisoformat(task.updated_at)
        except (ValueError, TypeError):
            continue

        age_secs = (now - updated).total_seconds()
        alive = is_task_alive(task.id)

        if not alive and age_secs > 30:
            # Process died but task still marked in_progress — stale
            logger.warning("Task %s process is dead (age %.0fs). Moving to failed.", task.id, age_secs)
            kill_task(task.id)  # Clean up PID file
            store.move_task(task, "failed")

        elif alive and age_secs > STUCK_TIMEOUT:
            # Process running too long — kill it
            logger.warning("Task %s exceeded timeout (%.0fs > %ds). Killing.", task.id, age_secs, STUCK_TIMEOUT)
            kill_task(task.id)
            store.move_task(task, "failed")


def _process_task(task: Task) -> None:
    """Process a single task (blocking). Called from a thread via run_in_executor."""
    try:
        if task.agent == "human":
            # Human tasks are never dispatched — they sit in blocked until responded to
            logger.warning("Human task %s should not be dispatched to an agent.", task.id)
            store.move_task(task, "blocked")
            return
        elif task.agent == "freebase":
            _handle_freebase(task)
        elif task.agent == "triage":
            _handle_triage(task)
        elif task.agent == "planner":
            _handle_planner(task)
        elif task.agent == "code_worker":
            _handle_code_worker(task)
        elif task.agent == "code_reviewer":
            _handle_code_reviewer(task)
        else:
            logger.error("Unknown agent type: %s", task.agent)
            store.move_task(task, "ready")
            return

        # After any agent handler, check for human_tasks in saved artifacts
        feature = store.load_feature(task.root_feature_id)
        if feature:
            _check_and_create_human_tasks(task, feature, task.id)

    except Exception:
        logger.exception("Error processing task %s", task.id)
        store.move_task(task, "failed")


def get_running_agents() -> dict[str, int]:
    """Return counts of currently running agents by type."""
    counts: dict[str, int] = {}
    for _, (_, agent) in _running.items():
        counts[agent] = counts.get(agent, 0) + 1
    return counts


async def run_loop() -> None:
    """Main supervisor polling loop with concurrent dispatch."""
    logger.info(
        "Supervisor started. Polling every %ds. Max concurrent: %d (workers: %d). Repo: %s",
        POLL_INTERVAL, MAX_CONCURRENT, MAX_CONCURRENT_WORKERS, REPO_PATH,
    )
    loop = asyncio.get_event_loop()

    # Clean up any stale worktrees from previous crashes
    await loop.run_in_executor(None, worktree.cleanup_stale_worktrees, REPO_PATH)

    while True:
        try:
            # Watchdog — runs on default executor, not the task pool
            await loop.run_in_executor(None, _check_stuck_tasks)

            # Reap finished tasks
            finished = [tid for tid, (fut, _) in _running.items() if fut.done()]
            for tid in finished:
                fut, agent = _running.pop(tid)
                try:
                    fut.result()
                except Exception:
                    logger.exception("Task %s (%s) raised after completion", tid, agent)
                else:
                    logger.info("Task %s (%s) completed.", tid, agent)

            # How many slots available?
            available = MAX_CONCURRENT - len(_running)
            if available <= 0:
                logger.debug("All %d slots occupied, waiting.", MAX_CONCURRENT)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Count running workers and freebase
            running_workers = sum(1 for _, (_, a) in _running.items() if a == "code_worker")
            running_freebase = sum(1 for _, (_, a) in _running.items() if a == "freebase")

            ready = await loop.run_in_executor(None, store.get_ready_tasks)
            if ready:
                logger.info("%d ready task(s), %d slot(s) available, %d running.",
                            len(ready), available, len(_running))
            dispatched = 0

            for task in ready:
                if available <= 0:
                    break
                if task.id in _running:
                    continue
                if task.agent == "human":
                    continue  # human tasks are never auto-dispatched
                if task.agent == "freebase" and running_freebase >= MAX_CONCURRENT_FREEBASE:
                    continue  # merges are serialized
                if task.agent == "code_worker" and running_workers >= MAX_CONCURRENT_WORKERS:
                    logger.debug("Skipping %s — worker limit reached (%d/%d).",
                                 task.id, running_workers, MAX_CONCURRENT_WORKERS)
                    continue

                # Move to in_progress before dispatching
                store.move_task(task, "in_progress")

                # Dispatch on the dedicated task executor
                fut = loop.run_in_executor(_task_executor, _process_task, task)
                _running[task.id] = (fut, task.agent)

                logger.info("Dispatched %s (agent: %s). Slots used: %d/%d",
                            task.id, task.agent, len(_running), MAX_CONCURRENT,
                            extra={"task_id": task.id, "feature_id": task.root_feature_id, "agent": task.agent})

                available -= 1
                dispatched += 1
                if task.agent == "code_worker":
                    running_workers += 1
                elif task.agent == "freebase":
                    running_freebase += 1

            if dispatched:
                logger.info("Dispatched %d task(s) this cycle. Total running: %d",
                            dispatched, len(_running))

        except Exception:
            logger.exception("Supervisor loop error")

        await asyncio.sleep(POLL_INTERVAL)
