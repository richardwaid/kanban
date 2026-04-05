"""Tests for server.py FastAPI endpoints."""

from __future__ import annotations

import controlplane.store as store
from controlplane.models import FeatureStatus, TaskStatus


# ---------------------------------------------------------------------------
# POST /api/features
# ---------------------------------------------------------------------------

class TestCreateFeature:
    def test_creates_feature_and_returns_response(self, test_client):
        resp = test_client.post("/api/features", json={
            "title": "Add dark mode",
            "description": "Users want a dark theme",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Add dark mode"
        assert body["description"] == "Users want a dark theme"
        assert body["id"].startswith("FEATURE-")
        assert body["status"] == FeatureStatus.PLANNING.value
        assert body["kind"] == "feature"

    def test_returns_feature_response_schema(self, test_client):
        resp = test_client.post("/api/features", json={
            "title": "T",
            "description": "D",
        })
        body = resp.json()
        for field in ("id", "title", "description", "kind", "status",
                      "current_task_id", "latest_commit_id", "iteration_count",
                      "created_at", "updated_at"):
            assert field in body, f"Missing field: {field}"

    def test_default_priority_is_high(self, test_client):
        test_client.post("/api/features", json={"title": "T", "description": "D"})
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].priority == "high"

    def test_custom_priority_accepted(self, test_client):
        test_client.post("/api/features", json={
            "title": "T", "description": "D", "priority": "low"
        })
        tasks = store.list_tasks()
        assert tasks[0].priority == "low"

    def test_persists_to_store(self, test_client):
        resp = test_client.post("/api/features", json={"title": "T", "description": "D"})
        feature_id = resp.json()["id"]
        feature = store.load_feature(feature_id)
        assert feature is not None
        assert feature.title == "T"


# ---------------------------------------------------------------------------
# POST /api/bug-reports
# ---------------------------------------------------------------------------

class TestCreateBugReport:
    def test_creates_bug_report(self, test_client):
        resp = test_client.post("/api/bug-reports", json={
            "title": "Login crash",
            "description": "Steps to reproduce",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"].startswith("BUG-")
        assert body["kind"] == "bug_report"
        assert body["status"] == FeatureStatus.TRIAGING.value

    def test_creates_triage_task(self, test_client):
        resp = test_client.post("/api/bug-reports", json={
            "title": "Login crash",
            "description": "Steps to reproduce",
        })
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].agent == "triage"
        assert tasks[0].type == "triage"


# ---------------------------------------------------------------------------
# GET /api/features/{id}
# ---------------------------------------------------------------------------

class TestGetFeature:
    def test_returns_feature(self, test_client):
        create_resp = test_client.post("/api/features", json={"title": "T", "description": "D"})
        feature_id = create_resp.json()["id"]

        resp = test_client.get(f"/api/features/{feature_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == feature_id

    def test_returns_404_for_missing(self, test_client):
        resp = test_client.get("/api/features/FEATURE-9999")
        assert resp.status_code == 404

    def test_404_detail_message(self, test_client):
        resp = test_client.get("/api/features/FEATURE-9999")
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/board
# ---------------------------------------------------------------------------

class TestGetBoard:
    def test_empty_board_has_correct_structure(self, test_client):
        resp = test_client.get("/api/board")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("queue", "active", "blocked", "failed", "done",
                    "done_total", "features", "agents", "max_concurrent",
                    "total_cost_usd", "total_tasks"):
            assert key in body, f"Missing board key: {key}"

    def test_board_queue_contains_new_task(self, test_client):
        test_client.post("/api/features", json={"title": "T", "description": "D"})
        resp = test_client.get("/api/board")
        body = resp.json()
        assert len(body["queue"]) == 1
        assert body["queue"][0]["agent"] == "planner"

    def test_board_features_list_populated(self, test_client):
        test_client.post("/api/features", json={"title": "Widget", "description": "D"})
        test_client.post("/api/features", json={"title": "Gadget", "description": "D"})
        resp = test_client.get("/api/board")
        assert len(resp.json()["features"]) == 2

    def test_board_done_total_reflects_done_tasks(self, test_client):
        create_resp = test_client.post("/api/features", json={"title": "T", "description": "D"})
        feature_id = create_resp.json()["id"]

        # Move the planner task to done directly via store
        tasks = store.list_tasks(status="ready")
        assert len(tasks) == 1
        store.move_task(tasks[0], "done")

        resp = test_client.get("/api/board")
        assert resp.json()["done_total"] == 1

    def test_board_filter_by_feature_id(self, test_client):
        r1 = test_client.post("/api/features", json={"title": "F1", "description": "D"})
        test_client.post("/api/features", json={"title": "F2", "description": "D"})
        fid = r1.json()["id"]

        resp = test_client.get(f"/api/board?feature_id={fid}")
        body = resp.json()
        # Only tasks for fid should appear
        all_tasks = body["queue"] + body["active"] + body["blocked"] + body["failed"] + body["done"]
        for t in all_tasks:
            assert t["root_feature_id"] == fid


# ---------------------------------------------------------------------------
# POST /api/features/{id}/approve
# ---------------------------------------------------------------------------

class TestApproveFeature:
    def _feature_awaiting_approval(self, store_module):
        """Helper: create a feature and manually move it to awaiting_approval."""
        feature, task = store_module.create_feature("Widget", "Build it")
        store_module.move_task(task, "done")
        feature.status = FeatureStatus.AWAITING_APPROVAL.value
        feature.current_task_id = None
        store_module.save_feature(feature)
        return feature

    def test_approve_transitions_to_in_progress(self, test_client):
        feature = self._feature_awaiting_approval(store)
        resp = test_client.post(f"/api/features/{feature.id}/approve")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        updated = store.load_feature(feature.id)
        assert updated.status == FeatureStatus.IN_PROGRESS.value

    def test_approve_creates_code_worker_task(self, test_client):
        feature = self._feature_awaiting_approval(store)
        resp = test_client.post(f"/api/features/{feature.id}/approve")
        task_id = resp.json()["task_id"]
        task = store.load_task(task_id)
        assert task is not None
        assert task.agent == "code_worker"
        assert task.type == "implement"

    def test_approve_returns_404_for_missing_feature(self, test_client):
        resp = test_client.post("/api/features/FEATURE-9999/approve")
        assert resp.status_code == 404

    def test_approve_returns_400_when_not_awaiting_approval(self, test_client):
        resp_create = test_client.post("/api/features", json={"title": "T", "description": "D"})
        fid = resp_create.json()["id"]
        # Feature is in planning, not awaiting_approval
        resp = test_client.post(f"/api/features/{fid}/approve")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/features/{id}/abandon
# ---------------------------------------------------------------------------

class TestAbandonFeature:
    def test_abandon_marks_feature_done(self, test_client):
        resp_create = test_client.post("/api/features", json={"title": "T", "description": "D"})
        fid = resp_create.json()["id"]

        resp = test_client.post(f"/api/features/{fid}/abandon")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        feature = store.load_feature(fid)
        assert feature.status == FeatureStatus.DONE.value
        assert feature.current_task_id is None

    def test_abandon_moves_open_tasks_to_done(self, test_client):
        resp_create = test_client.post("/api/features", json={"title": "T", "description": "D"})
        fid = resp_create.json()["id"]

        test_client.post(f"/api/features/{fid}/abandon")

        tasks = store.list_tasks(status="done")
        feature_tasks = [t for t in tasks if t.root_feature_id == fid]
        assert len(feature_tasks) >= 1

    def test_abandon_returns_404_for_missing(self, test_client):
        resp = test_client.post("/api/features/FEATURE-9999/abandon")
        assert resp.status_code == 404

    def test_abandon_returns_400_when_already_done(self, test_client):
        resp_create = test_client.post("/api/features", json={"title": "T", "description": "D"})
        fid = resp_create.json()["id"]
        test_client.post(f"/api/features/{fid}/abandon")

        # Second abandon should fail
        resp = test_client.post(f"/api/features/{fid}/abandon")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/features/{id}/revise-plan
# ---------------------------------------------------------------------------

class TestRevisePlan:
    def _feature_awaiting_approval(self):
        feature, task = store.create_feature("Widget", "Build it")
        store.move_task(task, "done")
        store.save_planner_result(task.id, {
            "task_id": task.id, "status": "done", "plan": "Step 1\nStep 2",
        })
        feature.status = FeatureStatus.AWAITING_APPROVAL.value
        feature.current_task_id = None
        store.save_feature(feature)
        return feature

    def test_revise_plan_creates_new_planner_task(self, test_client):
        feature = self._feature_awaiting_approval()
        resp = test_client.post(
            f"/api/features/{feature.id}/revise-plan",
            json={"feedback": "The plan is missing error handling"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "task_id" in body

        task = store.load_task(body["task_id"])
        assert task.agent == "planner"
        assert task.is_continuation is True

    def test_revise_plan_returns_feature_to_planning(self, test_client):
        feature = self._feature_awaiting_approval()
        test_client.post(
            f"/api/features/{feature.id}/revise-plan",
            json={"feedback": "needs more detail"},
        )
        updated = store.load_feature(feature.id)
        assert updated.status == FeatureStatus.PLANNING.value

    def test_revise_plan_stores_feedback_as_artifact(self, test_client):
        feature = self._feature_awaiting_approval()
        resp = test_client.post(
            f"/api/features/{feature.id}/revise-plan",
            json={"feedback": "add retry logic"},
        )
        task_id = resp.json()["task_id"]
        artifact = store.load_artifact("planner_results", task_id)
        assert artifact is not None
        assert artifact["feedback"] == "add retry logic"

    def test_revise_plan_returns_400_when_not_awaiting(self, test_client):
        resp_create = test_client.post("/api/features", json={"title": "T", "description": "D"})
        fid = resp_create.json()["id"]
        resp = test_client.post(
            f"/api/features/{fid}/revise-plan",
            json={"feedback": "feedback"},
        )
        assert resp.status_code == 400

    def test_revise_plan_returns_404_for_missing(self, test_client):
        resp = test_client.post(
            "/api/features/FEATURE-9999/revise-plan",
            json={"feedback": "feedback"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/tasks/{id}/respond
# ---------------------------------------------------------------------------

class TestRespondToTask:
    def _human_task(self):
        """Create a blocked human task for testing respond."""
        from controlplane.models import Task
        feature, _ = store.create_feature("Widget", "Build it")
        tid = store._next_task_id()
        human = Task(
            id=tid,
            root_feature_id=feature.id,
            agent="human",
            type="question",
            title="What colour?",
            description="Should it be red or blue?",
            status="blocked",
            priority="high",
        )
        store.save_task(human)
        feature.status = FeatureStatus.IN_PROGRESS.value
        feature.current_task_id = tid
        store.save_feature(feature)
        return feature, human

    def test_respond_creates_follow_up_worker(self, test_client):
        feature, human = self._human_task()
        resp = test_client.post(
            f"/api/tasks/{human.id}/respond",
            json={"response": "Make it blue"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

        follow_up = store.load_task(body["task_id"])
        assert follow_up.agent == "code_worker"
        assert follow_up.is_continuation is True

    def test_respond_marks_human_task_done(self, test_client):
        _, human = self._human_task()
        test_client.post(f"/api/tasks/{human.id}/respond", json={"response": "Blue"})
        updated = store.load_task(human.id)
        assert updated.status == "done"

    def test_respond_returns_404_for_missing_task(self, test_client):
        resp = test_client.post("/api/tasks/TASK-9999/respond", json={"response": "x"})
        assert resp.status_code == 404

    def test_respond_returns_400_for_non_human_task(self, test_client):
        _, planner_task = store.create_feature("Widget", "Build it")
        resp = test_client.post(
            f"/api/tasks/{planner_task.id}/respond",
            json={"response": "some answer"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/tasks/{id}/retry
# ---------------------------------------------------------------------------

class TestRetryTask:
    def test_retry_moves_failed_task_to_ready(self, test_client):
        _, task = store.create_feature("Widget", "Build it")
        store.move_task(task, "failed")

        resp = test_client.post(f"/api/tasks/{task.id}/retry")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        updated = store.load_task(task.id)
        assert updated.status == "ready"

    def test_retry_returns_404_for_missing(self, test_client):
        resp = test_client.post("/api/tasks/TASK-9999/retry")
        assert resp.status_code == 404

    def test_retry_returns_400_when_not_failed(self, test_client):
        _, task = store.create_feature("Widget", "Build it")
        # Task is in ready, not failed
        resp = test_client.post(f"/api/tasks/{task.id}/retry")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/settings and POST /api/settings/janky-mode
# ---------------------------------------------------------------------------

class TestSettings:
    """
    _janky_mode is a module-level global in server.py — it persists across
    tests in the same process.  Each test resets it to False at the start via
    monkeypatch so that tests are order-independent.
    """

    def test_get_settings_returns_janky_mode(self, test_client, monkeypatch):
        import controlplane.server as server
        monkeypatch.setattr(server, "_janky_mode", False)
        resp = test_client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert "janky_mode" in body
        assert body["janky_mode"] is False

    def test_toggle_janky_mode_turns_on(self, test_client, monkeypatch):
        import controlplane.server as server
        monkeypatch.setattr(server, "_janky_mode", False)
        resp = test_client.post("/api/settings/janky-mode")
        assert resp.status_code == 200
        assert resp.json()["janky_mode"] is True

    def test_toggle_janky_mode_twice_turns_off(self, test_client, monkeypatch):
        import controlplane.server as server
        monkeypatch.setattr(server, "_janky_mode", False)
        test_client.post("/api/settings/janky-mode")
        resp = test_client.post("/api/settings/janky-mode")
        assert resp.json()["janky_mode"] is False

    def test_get_settings_reflects_toggle(self, test_client, monkeypatch):
        import controlplane.server as server
        monkeypatch.setattr(server, "_janky_mode", False)
        test_client.post("/api/settings/janky-mode")
        resp = test_client.get("/api/settings")
        assert resp.json()["janky_mode"] is True
