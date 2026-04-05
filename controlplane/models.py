"""Data models for features and tasks."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


class FeatureKind(str, enum.Enum):
    """Discriminator for feature vs. bug report entries."""

    FEATURE = "feature"
    BUG_REPORT = "bug_report"


class FeatureStatus(str, enum.Enum):
    """Lifecycle states for a feature or bug report.

    Features follow: pending -> planning -> awaiting_approval -> in_progress -> merging -> done.
    Bugs follow: pending -> triaging -> (needs_info | in_progress) -> merging -> done.
    """

    PENDING = "pending"
    PLANNING = "planning"
    TRIAGING = "triaging"
    NEEDS_INFO = "needs_info"
    AWAITING_APPROVAL = "awaiting_approval"
    IN_PROGRESS = "in_progress"
    MERGING = "merging"
    DONE = "done"


class TaskStatus(str, enum.Enum):
    """Lifecycle states for a task. Tasks move between status-based filesystem directories."""

    READY = "ready"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"  # waiting for human action (approval, review, etc.)
    FAILED = "failed"    # failed and won't be retried automatically
    DONE = "done"


class AgentType(str, enum.Enum):
    """Types of agents that can execute tasks. Each maps to a Claude CLI invocation
    with a specific prompt template except 'human', which represents user action."""

    PLANNER = "planner"
    TRIAGE = "triage"
    CODE_WORKER = "code_worker"
    CODE_REVIEWER = "code_reviewer"
    FREEBASE = "freebase"
    HUMAN = "human"


class TaskType(str, enum.Enum):
    """Semantic type of work a task represents, determining which handler runs it."""

    PLAN = "plan"
    TRIAGE = "triage"
    IMPLEMENT = "implement"
    REVIEW = "review"
    MERGE = "merge"
    QUESTION = "question"


class ReviewOutcome(str, enum.Enum):
    """Possible outcomes from a code review. NEEDS_CHANGES triggers a follow-up worker task."""

    APPROVED = "approved"
    NEEDS_CHANGES = "needs_changes"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TriageVerdict(str, enum.Enum):
    """Possible outcomes from bug report triage. NEEDS_INFO parks the bug for user input."""

    VALID = "valid"
    NEEDS_INFO = "needs_info"


@dataclass
class Feature:
    """A feature request or bug report. Top-level work item that owns one or more tasks.

    Features are persisted as YAML files in data/features/ and identified by
    FEATURE-XXXX or BUG-XXXX IDs. The status field tracks position in the
    lifecycle state machine. current_task_id points to the active task, if any.
    """

    id: str
    title: str
    description: str
    kind: str = FeatureKind.FEATURE.value
    status: str = FeatureStatus.PENDING.value
    current_task_id: str | None = None
    latest_commit_id: str | None = None
    iteration_count: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    """A unit of work dispatched to an agent. Tasks are persisted as YAML files in
    status-based subdirectories (data/tasks/{ready,in_progress,blocked,failed,done}/).

    The agent field determines which handler processes the task. is_continuation
    signals that the Claude CLI should use --continue to resume a prior conversation.
    branch_name is set after a code_worker runs, linking the task to its git branch.
    """

    id: str
    root_feature_id: str
    agent: str  # code_worker | code_reviewer
    type: str  # implement | review
    title: str
    description: str
    status: str = TaskStatus.READY.value
    priority: str = "high"
    iteration: int = 1
    parent_task_id: str | None = None
    review_commit_id: str | None = None
    branch_name: str | None = None
    is_continuation: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkerResult:
    """Structured result from a code_worker agent. Contains the commit ID of the
    implementation and an optional list of human_tasks (questions for the user)."""

    task_id: str
    status: str
    commit_id: str
    summary: str
    human_tasks: list[dict] = field(default_factory=list)


@dataclass
class ReviewItem:
    """A single finding from a code review. Items are ranked by priority (lower is
    higher priority). Type is 'bug' or 'improvement' — bugs always take precedence."""

    priority: int
    title: str
    description: str
    type: str = "improvement"  # bug | improvement


@dataclass
class PlannerResult:
    """Structured result from a planner agent. Contains the implementation plan text
    that is shown to the user for approval or revision."""

    task_id: str
    status: str
    plan: str
    human_tasks: list[dict] = field(default_factory=list)


@dataclass
class TriageResult:
    """Structured result from a triage agent. The verdict determines whether the bug
    proceeds to implementation (valid) or is parked for more information (needs_info)."""

    task_id: str
    status: str
    verdict: str  # valid | needs_info
    summary: str
    questions: list[str] = field(default_factory=list)
    human_tasks: list[dict] = field(default_factory=list)


@dataclass
class FreebaseResult:
    """Structured result from a freebase merge agent. Status is 'done' on successful
    merge or 'conflict' if the agent could not resolve merge conflicts."""

    task_id: str
    status: str  # done | conflict
    merge_commit_id: str
    summary: str
    human_tasks: list[dict] = field(default_factory=list)


@dataclass
class ReviewResult:
    """Structured result from a code_reviewer agent. Contains the review outcome,
    a summary, and a list of ReviewItem findings ranked by priority."""

    task_id: str
    status: str
    review_outcome: str  # approved | needs_changes
    summary: str
    items: list[ReviewItem] = field(default_factory=list)
    human_tasks: list[dict] = field(default_factory=list)
