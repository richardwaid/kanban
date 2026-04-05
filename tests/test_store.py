"""Tests for store.py — filesystem-backed persistence."""

from __future__ import annotations

import json

import pytest
import yaml

import controlplane.store as store
from controlplane.models import FeatureKind, FeatureStatus, TaskStatus


# ---------------------------------------------------------------------------
# create_feature
# ---------------------------------------------------------------------------

class TestCreateFeature:
    def test_creates_feature_yaml(self, tmp_data_dir):
        feature, task = store.create_feature("Add search", "Users need search")
        yaml_path = store.FEATURES_DIR / f"{feature.id}.yaml"
        assert yaml_path.exists(), "Feature YAML not written"

    def test_feature_yaml_round_trips(self, tmp_data_dir):
        feature, _ = store.create_feature("Add search", "Users need search")
        data = yaml.safe_load((store.FEATURES_DIR / f"{feature.id}.yaml").read_text())
        reloaded = store.load_feature(feature.id)
        assert reloaded.title == "Add search"
        assert reloaded.description == "Users need search"

    def test_creates_planner_task_in_ready(self, tmp_data_dir):
        feature, task = store.create_feature("Add search", "Users need search")
        task_path = store.TASKS_BASE / "ready" / f"{task.id}.yaml"
        assert task_path.exists(), "Planner task YAML not in ready/"

    def test_planner_task_agent_and_type(self, tmp_data_dir):
        _, task = store.create_feature("Add search", "Users need search")
        assert task.agent == "planner"
        assert task.type == "plan"

    def test_feature_status_is_planning(self, tmp_data_dir):
        feature, _ = store.create_feature("Add search", "Users need search")
        assert feature.status == FeatureStatus.PLANNING.value

    def test_feature_id_has_feature_prefix(self, tmp_data_dir):
        feature, _ = store.create_feature("X", "Y")
        assert feature.id.startswith("FEATURE-")

    def test_feature_kind_is_feature(self, tmp_data_dir):
        feature, _ = store.create_feature("X", "Y")
        assert feature.kind == FeatureKind.FEATURE.value

    def test_current_task_id_matches_task(self, tmp_data_dir):
        feature, task = store.create_feature("X", "Y")
        assert feature.current_task_id == task.id


# ---------------------------------------------------------------------------
# create_bug_report
# ---------------------------------------------------------------------------

class TestCreateBugReport:
    def test_creates_bug_yaml(self, tmp_data_dir):
        feature, task = store.create_bug_report("Login crash", "Steps to reproduce")
        yaml_path = store.FEATURES_DIR / f"{feature.id}.yaml"
        assert yaml_path.exists()

    def test_bug_id_has_bug_prefix(self, tmp_data_dir):
        feature, _ = store.create_bug_report("Login crash", "Steps")
        assert feature.id.startswith("BUG-")

    def test_bug_kind(self, tmp_data_dir):
        feature, _ = store.create_bug_report("Login crash", "Steps")
        assert feature.kind == FeatureKind.BUG_REPORT.value

    def test_bug_status_is_triaging(self, tmp_data_dir):
        feature, _ = store.create_bug_report("Login crash", "Steps")
        assert feature.status == FeatureStatus.TRIAGING.value

    def test_creates_triage_task(self, tmp_data_dir):
        _, task = store.create_bug_report("Login crash", "Steps")
        assert task.agent == "triage"
        assert task.type == "triage"

    def test_triage_task_in_ready(self, tmp_data_dir):
        _, task = store.create_bug_report("Login crash", "Steps")
        task_path = store.TASKS_BASE / "ready" / f"{task.id}.yaml"
        assert task_path.exists()


# ---------------------------------------------------------------------------
# load_feature / load_task round-trips
# ---------------------------------------------------------------------------

class TestRoundTrips:
    def test_load_feature_returns_none_for_missing(self, tmp_data_dir):
        assert store.load_feature("FEATURE-9999") is None

    def test_load_feature_returns_correct_data(self, tmp_data_dir):
        feature, _ = store.create_feature("Widget", "Build it")
        loaded = store.load_feature(feature.id)
        assert loaded.id == feature.id
        assert loaded.title == "Widget"

    def test_load_task_returns_none_for_missing(self, tmp_data_dir):
        assert store.load_task("TASK-9999") is None

    def test_load_task_finds_task_in_ready(self, tmp_data_dir):
        _, task = store.create_feature("Widget", "Build it")
        loaded = store.load_task(task.id)
        assert loaded is not None
        assert loaded.id == task.id

    def test_load_task_finds_task_after_move(self, tmp_data_dir):
        _, task = store.create_feature("Widget", "Build it")
        store.move_task(task, "in_progress")
        loaded = store.load_task(task.id)
        assert loaded.status == "in_progress"


# ---------------------------------------------------------------------------
# move_task
# ---------------------------------------------------------------------------

class TestMoveTask:
    def test_move_task_changes_directory(self, tmp_data_dir):
        _, task = store.create_feature("Widget", "Build it")
        old_path = store.TASKS_BASE / "ready" / f"{task.id}.yaml"
        assert old_path.exists()

        store.move_task(task, "in_progress")

        assert not old_path.exists(), "Old path should be gone"
        new_path = store.TASKS_BASE / "in_progress" / f"{task.id}.yaml"
        assert new_path.exists()

    def test_move_task_updates_status_in_yaml(self, tmp_data_dir):
        _, task = store.create_feature("Widget", "Build it")
        store.move_task(task, "done")
        loaded = store.load_task(task.id)
        assert loaded.status == "done"

    def test_move_task_through_all_statuses(self, tmp_data_dir):
        _, task = store.create_feature("Widget", "Build it")
        for status in ("in_progress", "blocked", "failed", "done"):
            store.move_task(task, status)
            loaded = store.load_task(task.id)
            assert loaded.status == status


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

class TestListTasks:
    def test_list_tasks_all(self, tmp_data_dir):
        store.create_feature("F1", "D1")
        store.create_feature("F2", "D2")
        tasks = store.list_tasks()
        assert len(tasks) == 2

    def test_list_tasks_with_status_filter(self, tmp_data_dir):
        _, task1 = store.create_feature("F1", "D1")
        _, task2 = store.create_feature("F2", "D2")
        store.move_task(task1, "in_progress")

        ready = store.list_tasks(status="ready")
        in_progress = store.list_tasks(status="in_progress")

        assert len(ready) == 1
        assert ready[0].id == task2.id
        assert len(in_progress) == 1
        assert in_progress[0].id == task1.id

    def test_list_tasks_empty_when_none_exist(self, tmp_data_dir):
        assert store.list_tasks() == []

    def test_list_tasks_done_filter(self, tmp_data_dir):
        _, task = store.create_feature("F1", "D1")
        store.move_task(task, "done")
        done = store.list_tasks(status="done")
        assert len(done) == 1


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

class TestIdGeneration:
    def test_next_feature_id_starts_at_0001(self, tmp_data_dir):
        assert store._next_feature_id() == "FEATURE-0001"

    def test_next_feature_id_increments(self, tmp_data_dir):
        store.create_feature("F1", "D1")
        store.create_feature("F2", "D2")
        assert store._next_feature_id() == "FEATURE-0003"

    def test_next_bug_id_starts_at_0001(self, tmp_data_dir):
        assert store._next_bug_id() == "BUG-0001"

    def test_next_bug_id_increments(self, tmp_data_dir):
        store.create_bug_report("B1", "D1")
        store.create_bug_report("B2", "D2")
        assert store._next_bug_id() == "BUG-0003"

    def test_next_task_id_starts_at_0001(self, tmp_data_dir):
        assert store._next_task_id() == "TASK-0001"

    def test_next_task_id_increments_across_status_dirs(self, tmp_data_dir):
        _, task = store.create_feature("F1", "D1")
        store.move_task(task, "done")
        # TASK-0001 now lives in done; next should be TASK-0002
        assert store._next_task_id() == "TASK-0002"

    def test_feature_and_bug_ids_are_independent(self, tmp_data_dir):
        f, _ = store.create_feature("F1", "D1")
        b, _ = store.create_bug_report("B1", "D1")
        assert f.id == "FEATURE-0001"
        assert b.id == "BUG-0001"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

class TestArtifacts:
    def test_save_and_load_planner_result(self, tmp_data_dir):
        payload = {"task_id": "TASK-0001", "status": "done", "plan": "Step 1"}
        store.save_planner_result("TASK-0001", payload)
        loaded = store.load_artifact("planner_results", "TASK-0001")
        assert loaded == payload

    def test_save_and_load_triage_result(self, tmp_data_dir):
        payload = {"task_id": "TASK-0001", "verdict": "valid", "summary": "OK"}
        store.save_triage_result("TASK-0001", payload)
        loaded = store.load_artifact("triage_results", "TASK-0001")
        assert loaded == payload

    def test_save_and_load_worker_result(self, tmp_data_dir):
        payload = {"task_id": "TASK-0001", "commit_id": "abc", "summary": "Done"}
        store.save_worker_result("TASK-0001", payload)
        loaded = store.load_artifact("worker_results", "TASK-0001")
        assert loaded == payload

    def test_save_and_load_review_result(self, tmp_data_dir):
        payload = {"task_id": "TASK-0001", "review_outcome": "approved", "summary": "LGTM"}
        store.save_review_result("TASK-0001", payload)
        loaded = store.load_artifact("review_results", "TASK-0001")
        assert loaded == payload

    def test_load_artifact_returns_none_for_missing_task(self, tmp_data_dir):
        result = store.load_artifact("planner_results", "TASK-9999")
        assert result is None

    def test_load_artifact_rejects_invalid_type(self, tmp_data_dir):
        # load_artifact must refuse unknown types to prevent path traversal
        result = store.load_artifact("../../etc/passwd", "TASK-0001")
        assert result is None

    def test_load_artifact_rejects_unknown_type(self, tmp_data_dir):
        result = store.load_artifact("not_a_real_type", "TASK-0001")
        assert result is None

    def test_valid_artifact_types_whitelist(self, tmp_data_dir):
        assert "planner_results" in store.VALID_ARTIFACT_TYPES
        assert "triage_results" in store.VALID_ARTIFACT_TYPES
        assert "worker_results" in store.VALID_ARTIFACT_TYPES
        assert "review_results" in store.VALID_ARTIFACT_TYPES

    def test_artifact_written_as_json(self, tmp_data_dir):
        payload = {"k": "v"}
        store.save_planner_result("TASK-0001", payload)
        raw = (store.ARTIFACTS_DIR / "planner_results" / "TASK-0001.json").read_text()
        assert json.loads(raw) == payload
