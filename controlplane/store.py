"""Filesystem-backed persistence for features and tasks."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import yaml

from controlplane.models import (
    Feature,
    FeatureKind,
    FeatureStatus,
    Task,
    TaskStatus,
    _now,
)

DATA_DIR = Path(os.environ.get("KANBAN_DATA_DIR", "data"))

FEATURES_DIR = DATA_DIR / "features"
TASKS_BASE = DATA_DIR / "tasks"
ARTIFACTS_DIR = DATA_DIR / "artifacts"

VALID_ARTIFACT_TYPES = frozenset({
    "triage_results", "planner_results", "worker_results", "review_results",
})

_id_lock = threading.Lock()


def _ensure_dirs() -> None:
    for d in [
        FEATURES_DIR,
        TASKS_BASE / "ready",
        TASKS_BASE / "in_progress",
        TASKS_BASE / "blocked",
        TASKS_BASE / "failed",
        TASKS_BASE / "done",
        ARTIFACTS_DIR / "planner_results",
        ARTIFACTS_DIR / "triage_results",
        ARTIFACTS_DIR / "worker_results",
        ARTIFACTS_DIR / "review_results",
    ]:
        d.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


# --- ID generation (thread-safe) ---

def _next_feature_id() -> str:
    with _id_lock:
        existing = list(FEATURES_DIR.glob("FEATURE-*.yaml"))
        if not existing:
            return "FEATURE-0001"
        nums = [int(f.stem.split("-")[1]) for f in existing]
        return f"FEATURE-{max(nums) + 1:04d}"


def _next_bug_id() -> str:
    with _id_lock:
        existing = list(FEATURES_DIR.glob("BUG-*.yaml"))
        if not existing:
            return "BUG-0001"
        nums = [int(f.stem.split("-")[1]) for f in existing]
        return f"BUG-{max(nums) + 1:04d}"


def _next_task_id() -> str:
    with _id_lock:
        existing = []
        for sub in ["ready", "in_progress", "blocked", "failed", "done"]:
            existing.extend((TASKS_BASE / sub).glob("TASK-*.yaml"))
        if not existing:
            return "TASK-0001"
        nums = [int(f.stem.split("-")[1]) for f in existing]
        return f"TASK-{max(nums) + 1:04d}"


# --- Feature CRUD ---

def save_feature(feature: Feature) -> Feature:
    feature.updated_at = _now()
    path = FEATURES_DIR / f"{feature.id}.yaml"
    path.write_text(yaml.dump(feature.to_dict(), default_flow_style=False, sort_keys=False))
    return feature


def load_feature(feature_id: str) -> Feature | None:
    path = FEATURES_DIR / f"{feature_id}.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text())
    return Feature(**data)


def list_features() -> list[Feature]:
    features = []
    for path in sorted(FEATURES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        features.append(Feature(**data))
    return features


def create_feature(title: str, description: str, priority: str = "high") -> tuple[Feature, Task]:
    fid = _next_feature_id()
    tid = _next_task_id()

    feature = Feature(
        id=fid,
        title=title,
        description=description,
        status=FeatureStatus.PLANNING.value,
        current_task_id=tid,
    )

    task = Task(
        id=tid,
        root_feature_id=fid,
        agent="planner",
        type="plan",
        title=f"Plan: {title}",
        description=description,
        priority=priority,
    )

    save_feature(feature)
    save_task(task)
    return feature, task


def create_bug_report(title: str, description: str, priority: str = "high") -> tuple[Feature, Task]:
    fid = _next_bug_id()
    tid = _next_task_id()

    feature = Feature(
        id=fid,
        title=title,
        description=description,
        kind=FeatureKind.BUG_REPORT.value,
        status=FeatureStatus.TRIAGING.value,
        current_task_id=tid,
    )

    task = Task(
        id=tid,
        root_feature_id=fid,
        agent="triage",
        type="triage",
        title=f"Triage: {title}",
        description=description,
        priority=priority,
    )

    save_feature(feature)
    save_task(task)
    return feature, task


# --- Task CRUD ---

def _task_path(task: Task) -> Path:
    return TASKS_BASE / task.status / f"{task.id}.yaml"


def save_task(task: Task) -> Task:
    task.updated_at = _now()
    path = _task_path(task)
    path.write_text(yaml.dump(task.to_dict(), default_flow_style=False, sort_keys=False))
    return task


def load_task(task_id: str) -> Task | None:
    for sub in ["ready", "in_progress", "blocked", "failed", "done"]:
        path = TASKS_BASE / sub / f"{task_id}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text())
            return Task(**data)
    return None


def move_task(task: Task, new_status: str) -> Task:
    old_path = _task_path(task)
    task.status = new_status
    new_path = _task_path(task)
    if old_path.exists() and old_path != new_path:
        shutil.move(str(old_path), str(new_path))
    # Save to update status field in YAML
    save_task(task)
    return task


def list_tasks(status: str | None = None) -> list[Task]:
    tasks = []
    subdirs = [status] if status else ["ready", "in_progress", "blocked", "failed", "done"]
    for sub in subdirs:
        d = TASKS_BASE / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("TASK-*.yaml")):
            data = yaml.safe_load(path.read_text())
            tasks.append(Task(**data))
    return tasks


def get_ready_tasks() -> list[Task]:
    return list_tasks(status="ready")


# --- Artifacts ---

def save_artifact(artifact_type: str, task_id: str, data: dict) -> Path:
    """Save a task result artifact as JSON."""
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise ValueError(f"Invalid artifact type: {artifact_type}")
    path = ARTIFACTS_DIR / artifact_type / f"{task_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def save_triage_result(task_id: str, result: dict) -> Path:
    return save_artifact("triage_results", task_id, result)


def save_planner_result(task_id: str, result: dict) -> Path:
    return save_artifact("planner_results", task_id, result)


def save_worker_result(task_id: str, result: dict) -> Path:
    return save_artifact("worker_results", task_id, result)


def save_review_result(task_id: str, result: dict) -> Path:
    return save_artifact("review_results", task_id, result)


def load_artifact(artifact_type: str, task_id: str) -> dict | None:
    """Load a task result artifact. Returns None if not found or invalid type."""
    if artifact_type not in VALID_ARTIFACT_TYPES:
        return None
    path = ARTIFACTS_DIR / artifact_type / f"{task_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
