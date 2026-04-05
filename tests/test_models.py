"""Tests for models.py — dataclasses, enums, and to_dict() round-trips."""

from __future__ import annotations

from controlplane.models import (
    AgentType,
    Feature,
    FeatureKind,
    FeatureStatus,
    FreebaseResult,
    PlannerResult,
    ReviewItem,
    ReviewOutcome,
    ReviewResult,
    Task,
    TaskStatus,
    TaskType,
    TriageResult,
    TriageVerdict,
    WorkerResult,
)


# ---------------------------------------------------------------------------
# Enum string values
# ---------------------------------------------------------------------------

class TestEnumValues:
    def test_feature_status_values(self):
        assert FeatureStatus.PENDING.value == "pending"
        assert FeatureStatus.PLANNING.value == "planning"
        assert FeatureStatus.TRIAGING.value == "triaging"
        assert FeatureStatus.NEEDS_INFO.value == "needs_info"
        assert FeatureStatus.AWAITING_APPROVAL.value == "awaiting_approval"
        assert FeatureStatus.IN_PROGRESS.value == "in_progress"
        assert FeatureStatus.MERGING.value == "merging"
        assert FeatureStatus.DONE.value == "done"

    def test_task_status_values(self):
        assert TaskStatus.READY.value == "ready"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.BLOCKED.value == "blocked"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.DONE.value == "done"

    def test_agent_type_values(self):
        assert AgentType.PLANNER.value == "planner"
        assert AgentType.TRIAGE.value == "triage"
        assert AgentType.CODE_WORKER.value == "code_worker"
        assert AgentType.CODE_REVIEWER.value == "code_reviewer"
        assert AgentType.FREEBASE.value == "freebase"
        assert AgentType.HUMAN.value == "human"

    def test_task_type_values(self):
        assert TaskType.PLAN.value == "plan"
        assert TaskType.TRIAGE.value == "triage"
        assert TaskType.IMPLEMENT.value == "implement"
        assert TaskType.REVIEW.value == "review"
        assert TaskType.MERGE.value == "merge"
        assert TaskType.QUESTION.value == "question"

    def test_feature_kind_values(self):
        assert FeatureKind.FEATURE.value == "feature"
        assert FeatureKind.BUG_REPORT.value == "bug_report"

    def test_review_outcome_values(self):
        assert ReviewOutcome.APPROVED.value == "approved"
        assert ReviewOutcome.NEEDS_CHANGES.value == "needs_changes"

    def test_triage_verdict_values(self):
        assert TriageVerdict.VALID.value == "valid"
        assert TriageVerdict.NEEDS_INFO.value == "needs_info"

    def test_enums_are_str_subclass(self):
        # str-subclassed enums compare equal to their raw string value
        assert FeatureStatus.PENDING == "pending"
        assert TaskStatus.READY == "ready"
        assert FeatureKind.FEATURE == "feature"


# ---------------------------------------------------------------------------
# Feature creation and defaults
# ---------------------------------------------------------------------------

class TestFeature:
    def test_feature_defaults(self):
        f = Feature(id="FEATURE-0001", title="T", description="D")
        assert f.kind == FeatureKind.FEATURE.value
        assert f.status == FeatureStatus.PENDING.value
        assert f.current_task_id is None
        assert f.latest_commit_id is None
        assert f.iteration_count == 0
        assert f.created_at is not None
        assert f.updated_at is not None

    def test_feature_to_dict_round_trip(self):
        f = Feature(id="FEATURE-0001", title="T", description="D")
        d = f.to_dict()
        f2 = Feature(**d)
        assert f2.id == f.id
        assert f2.title == f.title
        assert f2.status == f.status
        assert f2.created_at == f.created_at

    def test_feature_to_dict_contains_all_fields(self):
        f = Feature(id="FEATURE-0001", title="T", description="D")
        d = f.to_dict()
        for field in ("id", "title", "description", "kind", "status",
                      "current_task_id", "latest_commit_id", "iteration_count",
                      "created_at", "updated_at"):
            assert field in d


# ---------------------------------------------------------------------------
# Task creation and defaults
# ---------------------------------------------------------------------------

class TestTask:
    def test_task_defaults(self):
        t = Task(
            id="TASK-0001",
            root_feature_id="FEATURE-0001",
            agent="code_worker",
            type="implement",
            title="Do the thing",
            description="Details",
        )
        assert t.status == TaskStatus.READY.value
        assert t.priority == "high"
        assert t.iteration == 1
        assert t.parent_task_id is None
        assert t.review_commit_id is None
        assert t.branch_name is None
        assert t.is_continuation is False

    def test_task_to_dict_round_trip(self):
        t = Task(
            id="TASK-0001",
            root_feature_id="FEATURE-0001",
            agent="planner",
            type="plan",
            title="Plan it",
            description="Details",
        )
        d = t.to_dict()
        t2 = Task(**d)
        assert t2.id == t.id
        assert t2.agent == t.agent
        assert t2.is_continuation == t.is_continuation


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

class TestResultDataclasses:
    def test_planner_result_defaults(self):
        r = PlannerResult(task_id="TASK-0001", status="done", plan="Step 1\nStep 2")
        assert r.human_tasks == []

    def test_worker_result_defaults(self):
        r = WorkerResult(task_id="TASK-0001", status="done", commit_id="abc123", summary="Done")
        assert r.human_tasks == []

    def test_review_result_defaults(self):
        r = ReviewResult(
            task_id="TASK-0001",
            status="done",
            review_outcome="approved",
            summary="LGTM",
        )
        assert r.items == []
        assert r.human_tasks == []

    def test_review_item_default_type(self):
        item = ReviewItem(priority=1, title="XSS", description="Missing escaping")
        assert item.type == "improvement"

    def test_review_item_explicit_type(self):
        item = ReviewItem(priority=1, title="XSS", description="Missing escaping", type="bug")
        assert item.type == "bug"

    def test_triage_result_defaults(self):
        r = TriageResult(task_id="TASK-0001", status="done", verdict="valid", summary="OK")
        assert r.questions == []
        assert r.human_tasks == []

    def test_freebase_result_defaults(self):
        r = FreebaseResult(
            task_id="TASK-0001", status="done", merge_commit_id="abc123", summary="Merged"
        )
        assert r.human_tasks == []
