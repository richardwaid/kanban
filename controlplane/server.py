"""FastAPI server for the Kanban board."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from controlplane import store
from controlplane.models import FeatureStatus, Task
from controlplane.runner import is_task_alive, kill_task
from controlplane.supervisor import run_loop, get_running_agents, MAX_CONCURRENT, REPO_PATH

import re


def _resolve_ticket_refs(text: str) -> str:
    """Scan text for ticket IDs (FEATURE-XXXX, BUG-XXXX) and append their context."""
    refs = set(re.findall(r'\b((?:FEATURE|BUG)-\d{4})\b', text))
    if not refs:
        return text

    context_parts = []
    for ref_id in sorted(refs):
        feat = store.load_feature(ref_id)
        if not feat:
            continue

        parts = [f"### Referenced: {ref_id} — {feat.title}"]
        parts.append(f"**Kind:** {feat.kind} | **Status:** {feat.status}")
        parts.append(f"**Description:** {feat.description}")

        # Find approved plan
        for t in store.list_tasks(status="done"):
            if t.root_feature_id == ref_id and t.agent == "planner":
                plan = store.load_artifact("planner_results", t.id)
                if plan and plan.get("plan"):
                    parts.append(f"**Approved Plan:**\n{plan['plan']}")
                    break

        # Find latest review result
        for t in reversed(store.list_tasks(status="done")):
            if t.root_feature_id == ref_id and t.agent == "code_reviewer":
                review = store.load_artifact("review_results", t.id)
                if review:
                    parts.append(f"**Latest Review:** {review.get('review_outcome', '')} — {review.get('summary', '')}")
                    break

        context_parts.append("\n".join(parts))

    if not context_parts:
        return text

    return text + "\n\n## Referenced Tickets\n\n" + "\n\n".join(context_parts)


from controlplane.logging_config import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent.parent / "ui"


_github_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _github_client
    tasks = []

    # Initialize GitHub client if configured
    from controlplane.github_client import create_client_from_env
    _github_client = create_client_from_env()
    if _github_client:
        logger.info("GitHub integration enabled for %s", _github_client.repo)
        # Setup repo (clone or configure remote)
        from controlplane.worktree import setup_repo
        await asyncio.get_event_loop().run_in_executor(None, setup_repo, REPO_PATH, _github_client)
        # Start issue poller
        from controlplane.github_sync import poll_github_issues
        tasks.append(asyncio.create_task(poll_github_issues(_github_client)))
    else:
        logger.info("GitHub integration not configured (local-only mode)")

    # Start the supervisor loop
    tasks.append(asyncio.create_task(run_loop()))
    logger.info("Supervisor loop started as background task")

    yield

    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Kanban Agents",
    description=(
        "Multi-agent orchestration system for autonomous software development. "
        "Dispatches Claude CLI agents to plan, implement, review, and merge code changes "
        "through a Kanban-style workflow."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# --- Janky mode ---
_janky_mode = False


# --- API models ---

class CreateFeatureRequest(BaseModel):
    title: str
    description: str
    priority: str = "high"


class FeatureResponse(BaseModel):
    id: str
    title: str
    description: str
    kind: str = "feature"
    status: str
    current_task_id: str | None
    latest_commit_id: str | None
    iteration_count: int
    github_issue_number: int | None = None
    github_pr_number: int | None = None
    github_pr_url: str | None = None
    created_at: str
    updated_at: str


class TaskResponse(BaseModel):
    id: str
    root_feature_id: str
    agent: str
    type: str
    title: str
    description: str
    status: str
    priority: str
    iteration: int
    parent_task_id: str | None
    review_commit_id: str | None
    branch_name: str | None
    created_at: str
    updated_at: str
    triage_result: dict | None = None
    planner_result: dict | None = None
    worker_result: dict | None = None
    review_result: dict | None = None


class BoardResponse(BaseModel):
    queue: list[TaskResponse]
    active: list[TaskResponse]
    blocked: list[TaskResponse]
    failed: list[TaskResponse]
    done: list[TaskResponse]
    done_total: int
    features: list[FeatureResponse]
    agents: dict[str, int]  # running agent counts by type
    max_concurrent: int
    total_cost_usd: float
    total_tasks: int


@app.get("/api/settings", tags=["Settings"])
def get_settings():
    return {"janky_mode": _janky_mode}


@app.get("/api/settings/github", tags=["Settings"])
def get_github_settings():
    return {
        "connected": _github_client is not None,
        "repo": _github_client.repo if _github_client else None,
    }


@app.post("/api/settings/janky-mode", tags=["Settings"])
def toggle_janky_mode():
    global _janky_mode
    _janky_mode = not _janky_mode
    logger.info("Janky mode: %s", _janky_mode)
    return {"janky_mode": _janky_mode}


# --- Endpoints ---

@app.post("/api/features", response_model=FeatureResponse, tags=["Features"])
def create_feature(req: CreateFeatureRequest):
    feature, task = store.create_feature(req.title, _resolve_ticket_refs(req.description), req.priority)
    logger.info("Created feature %s with initial task %s", feature.id, task.id)
    return FeatureResponse(**feature.to_dict())


@app.get("/api/features/{feature_id}", response_model=FeatureResponse, tags=["Features"])
def get_feature(feature_id: str):
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    return FeatureResponse(**feature.to_dict())


class CreateBugReportRequest(BaseModel):
    title: str
    description: str
    priority: str = "high"


@app.post("/api/bug-reports", response_model=FeatureResponse, tags=["Bugs"])
def create_bug_report(req: CreateBugReportRequest):
    feature, task = store.create_bug_report(req.title, _resolve_ticket_refs(req.description), req.priority)
    logger.info("Created bug report %s with triage task %s", feature.id, task.id)
    return FeatureResponse(**feature.to_dict())


class UpdateFeatureRequest(BaseModel):
    title: str | None = None
    description: str | None = None


@app.patch("/api/features/{feature_id}", response_model=FeatureResponse, tags=["Features"])
def update_feature(feature_id: str, req: UpdateFeatureRequest):
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    if req.title is not None:
        feature.title = req.title
    if req.description is not None:
        feature.description = _resolve_ticket_refs(req.description)
    store.save_feature(feature)
    logger.info("Updated feature %s", feature_id)
    return FeatureResponse(**feature.to_dict())


@app.post("/api/features/{feature_id}/resubmit", tags=["Bugs"])
def resubmit_feature(feature_id: str):
    """Re-trigger triage after user has added more info to a bug report."""
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    if feature.status != FeatureStatus.NEEDS_INFO.value:
        raise HTTPException(status_code=400, detail=f"Not in needs_info state (status: {feature.status})")

    tid = store._next_task_id()
    task = Task(
        id=tid,
        root_feature_id=feature.id,
        agent="triage",
        type="triage",
        title=f"Re-triage: {feature.title}",
        description=feature.description,
        priority="high",
    )
    store.save_task(task)

    feature.status = FeatureStatus.TRIAGING.value
    feature.current_task_id = tid
    store.save_feature(feature)

    # Sync updated description back to GitHub issue
    if _github_client and feature.github_issue_number:
        from controlplane.github_sync import sync_description_to_github
        sync_description_to_github(_github_client, feature)

    logger.info("Feature %s resubmitted for triage. Task %s", feature_id, tid)
    return {"ok": True, "task_id": tid}


@app.post("/api/features/{feature_id}/abandon", tags=["Features"])
def abandon_feature(feature_id: str):
    """Abandon a feature or bug report. Kills any running tasks, moves all to done."""
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    if feature.status == FeatureStatus.DONE.value:
        raise HTTPException(status_code=400, detail="Already done")

    # Kill/move any outstanding tasks for this feature
    for task in store.list_tasks():
        if task.root_feature_id != feature_id:
            continue
        if task.status in ("ready", "in_progress", "blocked", "failed"):
            kill_task(task.id)
            store.move_task(task, "done")

    feature.status = FeatureStatus.DONE.value
    feature.current_task_id = None
    store.save_feature(feature)

    # Close open PR on GitHub if applicable
    if _github_client and feature.github_pr_number:
        try:
            _github_client._request(
                "PATCH", f"/repos/{_github_client.repo}/pulls/{feature.github_pr_number}",
                json={"state": "closed"},
            )
            _github_client.add_comment(feature.github_pr_number, "Feature abandoned. Closing PR.")
            logger.info("Closed PR #%d for abandoned %s", feature.github_pr_number, feature_id)
        except Exception:
            logger.exception("Failed to close PR #%d", feature.github_pr_number)

    # Close linked GitHub issue if applicable
    if _github_client and feature.github_issue_number:
        from controlplane.github_sync import close_issue
        close_issue(_github_client, feature)

    logger.info("Feature %s abandoned", feature_id)
    return {"ok": True}


class RevisePlanRequest(BaseModel):
    feedback: str


@app.post("/api/features/{feature_id}/revise-plan", tags=["Features"])
def revise_plan(feature_id: str, req: RevisePlanRequest):
    """Send feedback on a plan, triggering a new planning iteration."""
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    if feature.status != FeatureStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail=f"Not awaiting approval (status: {feature.status})")

    # Find the current plan
    previous_plan = ""
    all_tasks = store.list_tasks(status="done")
    for t in all_tasks:
        if t.root_feature_id == feature.id and t.agent == "planner":
            plan_artifact = store.load_artifact("planner_results", t.id)
            if plan_artifact and plan_artifact.get("plan"):
                previous_plan = plan_artifact["plan"]

    # Save the feedback as an artifact so the supervisor can pass it to the runner
    tid = store._next_task_id()
    store.save_planner_result(tid, {
        "task_id": tid, "status": "revision_request",
        "plan": previous_plan, "feedback": _resolve_ticket_refs(req.feedback),
    })

    task = Task(
        id=tid,
        root_feature_id=feature.id,
        agent="planner",
        type="plan",
        title=f"Revise plan: {feature.title}",
        description=feature.description,
        priority="high",
        is_continuation=True,
    )
    store.save_task(task)

    feature.status = FeatureStatus.PLANNING.value
    feature.current_task_id = tid
    store.save_feature(feature)
    logger.info("Feature %s plan revision requested. Task %s", feature_id, tid)
    return {"ok": True, "task_id": tid}


@app.post("/api/features/{feature_id}/approve", tags=["Features"])
def approve_feature(feature_id: str):
    """Approve a planned feature, creating the initial code_worker task."""
    feature = store.load_feature(feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")
    if feature.status != FeatureStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail=f"Not awaiting approval (status: {feature.status})")

    # Find the approved plan to include in the worker task description
    description = feature.description
    all_tasks = store.list_tasks(status="done")
    for t in all_tasks:
        if t.root_feature_id == feature.id and t.agent == "planner":
            plan_artifact = store.load_artifact("planner_results", t.id)
            if plan_artifact and plan_artifact.get("plan"):
                description += f"\n\n## Approved Plan\n\n{plan_artifact['plan']}"
                break

    tid = store._next_task_id()
    task = Task(
        id=tid,
        root_feature_id=feature.id,
        agent="code_worker",
        type="implement",
        title=feature.title,
        description=description,
        priority="high",
    )
    store.save_task(task)

    feature.status = FeatureStatus.IN_PROGRESS.value
    feature.current_task_id = tid
    store.save_feature(feature)
    logger.info("Feature %s approved. Created worker task %s", feature_id, tid)
    return {"ok": True, "task_id": tid}


@app.get("/api/tasks/{task_id}", response_model=TaskResponse, tags=["Tasks"])
def get_task(task_id: str):
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    resp = TaskResponse(**task.to_dict())
    # Attach artifacts if available
    resp.triage_result = store.load_artifact("triage_results", task_id)
    resp.planner_result = store.load_artifact("planner_results", task_id)
    resp.worker_result = store.load_artifact("worker_results", task_id)
    resp.review_result = store.load_artifact("review_results", task_id)
    return resp


class MoveTaskRequest(BaseModel):
    column: str  # worker_ready, worker_in_progress, reviewer_ready, reviewer_in_progress, done


# Column name → (agent, status) mapping
_COLUMN_MAP = {
    "queue": (None, "ready"),
    "active": (None, "in_progress"),
    "blocked": (None, "blocked"),
    "failed": (None, "failed"),
    "done": (None, "done"),
}


@app.patch("/api/tasks/{task_id}/move", tags=["Tasks"])
def move_task(task_id: str, req: MoveTaskRequest):
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if req.column not in _COLUMN_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid column: {req.column}")

    agent, new_status = _COLUMN_MAP[req.column]
    if agent is not None:
        task.agent = agent
    store.move_task(task, new_status)
    logger.info("Moved task %s to column %s (status=%s)", task_id, req.column, new_status)
    return {"ok": True}


@app.get("/api/tasks/{task_id}/events", tags=["Tasks"])
def get_task_events(task_id: str, after: int = 0):
    """Return structured JSONL events for a task, starting from line `after`."""
    import json as _json
    log_path = store.DATA_DIR / "logs" / f"{task_id}.jsonl"
    events = []
    if log_path.exists():
        all_lines = log_path.read_text().splitlines()
        for line in all_lines[after:]:
            try:
                events.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
        total = len(all_lines)
    else:
        total = 0
    return {
        "task_id": task_id,
        "events": events,
        "total": total,
        "alive": is_task_alive(task_id),
    }


class RespondToTaskRequest(BaseModel):
    response: str


@app.post("/api/tasks/{task_id}/respond", tags=["Tasks"])
def respond_to_task(task_id: str, req: RespondToTaskRequest):
    """Respond to a human task. Creates a follow-up worker task with the answer."""
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.agent != "human":
        raise HTTPException(status_code=400, detail="Only human tasks can be responded to")
    if task.status not in ("blocked", "ready"):
        raise HTTPException(status_code=400, detail=f"Task is not awaiting response (status: {task.status})")

    feature = store.load_feature(task.root_feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Not found")

    # Mark human task as done
    store.move_task(task, "done")

    # Create a follow-up worker task with the human's answer — marked as continuation
    # so the worker resumes the previous conversation context via --continue
    tid = store._next_task_id()
    follow_up = Task(
        id=tid,
        root_feature_id=feature.id,
        agent="code_worker",
        type="implement",
        title=f"Continue: {task.title}",
        description=f"The human answered your question.\n\nQuestion: {task.description}\n\nAnswer: {_resolve_ticket_refs(req.response)}\n\nContinue with your implementation.",
        priority=task.priority,
        iteration=task.iteration,
        parent_task_id=task.parent_task_id,  # link back to the original agent task
        is_continuation=True,
    )
    store.save_task(follow_up)

    feature.status = FeatureStatus.IN_PROGRESS.value
    feature.current_task_id = tid
    store.save_feature(feature)
    logger.info("Human task %s responded. Created follow-up worker %s", task_id, tid)
    return {"ok": True, "task_id": tid}


@app.post("/api/tasks/{task_id}/kill", tags=["Tasks"])
def kill_task_endpoint(task_id: str):
    """Kill a running task and move it to failed."""
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    killed = kill_task(task_id)
    if task.status == "in_progress":
        store.move_task(task, "failed")
    return {"ok": True, "killed": killed}


@app.post("/api/tasks/{task_id}/retry", tags=["Tasks"])
def retry_task_endpoint(task_id: str):
    """Move a failed task back to ready for retry."""
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "failed":
        raise HTTPException(status_code=400, detail=f"Task is not failed (status: {task.status})")
    store.move_task(task, "ready")
    logger.info("Task %s moved back to ready for retry", task_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/abandon", tags=["Tasks"])
def abandon_task_endpoint(task_id: str):
    """Permanently abandon a task (kill if running, move to done)."""
    task = store.load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    kill_task(task_id)
    store.move_task(task, "done")
    logger.info("Task %s abandoned", task_id)
    return {"ok": True}


import json as _json
import time as _time
import threading as _threading

_cost_lock = _threading.Lock()
_cost_cache = {"total": 0.0, "count": 0, "files_seen": set(), "last_update": 0}

def _get_total_cost() -> tuple[float, int]:
    """Sum cost_usd from all task event logs. Incrementally cached, thread-safe."""
    with _cost_lock:
        cache = _cost_cache
        now = _time.monotonic()
        if now - cache["last_update"] < 10:
            return cache["total"], cache["count"]

        log_dir = store.DATA_DIR / "logs"
        if not log_dir.exists():
            return 0.0, 0

        # Reset if cache grows too large
        if len(cache["files_seen"]) > 10000:
            cache["total"] = 0.0
            cache["count"] = 0
            cache["files_seen"] = set()

        for path in log_dir.glob("*.jsonl"):
            if path.name in cache["files_seen"]:
                continue
            try:
                for line in path.read_text().splitlines():
                    try:
                        evt = _json.loads(line)
                        if evt.get("kind") == "result" and "cost_usd" in evt:
                            cache["total"] += evt["cost_usd"]
                            cache["count"] += 1
                    except _json.JSONDecodeError:
                        continue
            except OSError:
                continue
            cache["files_seen"].add(path.name)

        cache["last_update"] = now
        return cache["total"], cache["count"]


@app.get("/api/board", tags=["Board"])
def get_board(done_limit: int = 20, done_offset: int = 0, feature_id: str | None = None):
    all_tasks = store.list_tasks()
    features = store.list_features()

    queue = []
    active = []
    blocked = []
    failed = []
    done_all = []

    for task in all_tasks:
        if feature_id and task.root_feature_id != feature_id:
            continue

        resp = TaskResponse(**task.to_dict())
        resp.triage_result = store.load_artifact("triage_results", task.id)
        resp.planner_result = store.load_artifact("planner_results", task.id)
        resp.worker_result = store.load_artifact("worker_results", task.id)
        resp.review_result = store.load_artifact("review_results", task.id)

        if task.status == "done":
            done_all.append(resp)
        elif task.status == "in_progress":
            active.append(resp)
        elif task.status == "blocked":
            blocked.append(resp)
        elif task.status == "failed":
            failed.append(resp)
        else:
            queue.append(resp)

    # Done: newest first, paginated
    done_all.sort(key=lambda t: t.updated_at, reverse=True)
    done_total = len(done_all)
    done_page = done_all[done_offset:done_offset + done_limit]

    total_cost, total_tasks = _get_total_cost()

    return BoardResponse(
        queue=queue,
        active=active,
        blocked=blocked,
        failed=failed,
        done=done_page,
        done_total=done_total,
        features=[FeatureResponse(**f.to_dict()) for f in features],
        agents=get_running_agents(),
        max_concurrent=MAX_CONCURRENT,
        total_cost_usd=round(total_cost, 4),
        total_tasks=total_tasks,
    )


# --- Static UI ---

@app.get("/")
def serve_index():
    return FileResponse(UI_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")
