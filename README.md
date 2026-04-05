# Kanban Agents

Multi-agent orchestration system for autonomous software development. Creates
feature requests or bug reports, plans implementations, dispatches Claude CLI
agents to write code, reviews the results, and merges approved changes — all
through a browser-based Kanban board.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## Setup

```bash
uv sync
```

## Run

```bash
uv run uvicorn controlplane.server:app --reload --port 8000
```

Open http://localhost:8000 in your browser.

## How It Works

### Feature Flow

1. Create a feature request from the UI.
2. A **planner** agent reads the codebase and produces an implementation plan.
3. The plan is shown in the UI for human review (**awaiting_approval**).
4. Approve the plan, or send feedback to trigger a plan revision.
5. On approval, a **code_worker** agent implements the change in an isolated git worktree.
6. A **code_reviewer** agent reviews the commit against the approved plan.
7. If the reviewer finds bugs or requests changes, a new worker task is created
   from the top-priority review item. The loop repeats.
8. On approval, a **freebase** merge agent rebases and merges the feature branch
   into the default branch.

### Bug Reports and Triage

Bug reports follow a different entry path:

1. Create a bug report from the UI.
2. A **triage** agent evaluates the report: reads the codebase, checks whether
   the bug is reproducible, and determines if more information is needed.
3. If the triage verdict is **needs_info**, the bug is parked. The user can edit
   the description and resubmit for another triage round.
4. If the verdict is **valid**, the bug goes directly to a code_worker (no
   planning step). The rest of the flow is the same as features.

### Planning and Approval

The planner agent produces a structured implementation plan. After receiving the
plan, you can:

- **Approve**: creates a code_worker task with the approved plan embedded in the
  description.
- **Revise**: provide free-text feedback. A new planner task is created with
  `--continue` to resume the conversation, carrying the previous plan and your
  feedback. The planner must address the feedback and produce a revised plan.

Multiple revision rounds are supported.

### Human Tasks and Q&A

Any agent can emit `human_tasks` in its result JSON. These create blocked tasks
assigned to the `human` agent type. They appear in the "Your Turn" pane in the
UI.

When you respond to a human task, the system creates a follow-up code_worker
task marked as a continuation (`--continue`), so the worker resumes with full
conversation context including your answer.

### Git Worktree Isolation

Each feature gets a persistent git worktree at `repo/../.worktrees/FEATURE-XXXX`
on branch `work/FEATURE-XXXX`. All code_worker and code_reviewer tasks for the
same feature share this worktree. This provides:

- **Isolation**: multiple features can be implemented in parallel without
  interfering with each other or the main checkout.
- **Conversation continuity**: successive Claude CLI invocations use `--continue`
  to resume the previous conversation in the same worktree directory. The agent
  retains context across retries, review iterations, and human Q&A.

Freebase merge tasks use temporary task-scoped worktrees (`task/TASK-XXXX`)
that are cleaned up after the merge completes.

### Freebase Merge Agent

When a review is approved, the supervisor creates a freebase merge task. The
merge strategy is:

1. Attempt a programmatic rebase of the feature branch onto the default branch.
2. If the rebase is clean, fast-forward merge (or merge commit as fallback).
3. If the rebase has conflicts, invoke a Claude CLI agent to resolve them
   in a temporary worktree, then merge the resolved branch.

Freebase tasks are serialized (max 1 at a time) to avoid race conditions on the
default branch.

### Janky Mode

A toggle in the settings that injects deliberate bugs into code_worker output.
When active, there is a 50% chance each worker task receives an addendum
instructing it to introduce one subtle, catchable flaw (XSS, off-by-one, race
condition, etc.). Useful for testing whether the code_reviewer agent catches
realistic bugs.

Toggle via the UI settings or the API:

```bash
curl -X POST http://localhost:8000/api/settings/janky-mode
```

### Ticket References

Feature and bug descriptions can reference other tickets using `FEATURE-XXXX` or
`BUG-XXXX` syntax. The server resolves these references at creation time,
appending the referenced ticket's title, description, approved plan, and latest
review outcome to the description. This gives agents cross-ticket context.

## UI Overview

The web UI (`ui/index.html`) is a single-page application that polls the
`/api/board` endpoint. It provides:

- **Jira-style list view**: features grouped by status, with expandable details
  showing the current task, iteration count, and plan/triage results.
- **Your Turn pane**: surfaces blocked human tasks and features awaiting
  approval, so the operator knows what needs attention.
- **Task board tabs**: queue, active, blocked, failed, done columns with task
  cards showing agent type, iteration, and status.
- **Task detail view**: structured event log streamed from the JSONL file,
  showing tool calls, text output, and cost tracking.
- **Settings**: janky mode toggle, cost and task count summary.

## Project Structure

```
controlplane/
  server.py          # FastAPI server, REST API, lifespan management
  supervisor.py      # Polling loop, task dispatch, watchdog, agent handlers
  runner.py          # Claude CLI subprocess runner, stream-json parsing
  models.py          # Dataclass models (Feature, Task, result types)
  store.py           # Filesystem-backed persistence (YAML/JSON)
  worktree.py        # Git worktree management (per-feature isolation)
  prompts/
    planner.md       # Prompt template for the planner agent
    triage.md        # Prompt template for the triage agent
    code_worker.md   # Prompt template for code workers
    code_reviewer.md # Prompt template for code reviewers
    freebase.md      # Prompt template for the freebase merge agent

ui/
  index.html         # Single-page Kanban board UI

data/                # Runtime data (created automatically)
  features/          # Feature/bug YAML files (FEATURE-XXXX.yaml, BUG-XXXX.yaml)
  tasks/
    ready/           # Tasks waiting to be picked up
    in_progress/     # Tasks currently being executed
    blocked/         # Human tasks awaiting response
    failed/          # Failed tasks (retryable)
    done/            # Completed tasks
  artifacts/
    planner_results/ # JSON results from planner agents
    triage_results/  # JSON results from triage agents
    worker_results/  # JSON results from code workers / freebase
    review_results/  # JSON results from code reviewers
  logs/
    TASK-XXXX.jsonl  # Structured event log per task
    TASK-XXXX.pid    # PID file for running Claude CLI process

repo/                # Target git repository for agent work
```

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `KANBAN_DATA_DIR` | `data` | Path to data directory |
| `KANBAN_REPO_PATH` | `repo` | Path to target git repository |
| `KANBAN_POLL_INTERVAL` | `5` | Supervisor poll interval (seconds) |
| `KANBAN_STUCK_TIMEOUT` | `300` | Watchdog kill timeout (seconds) |
| `KANBAN_MAX_CONCURRENT` | `5` | Max concurrent agent tasks |
| `KANBAN_MAX_CONCURRENT_WORKERS` | `3` | Max concurrent code_worker tasks |
| `KANBAN_TASK_TIMEOUT` | `300` | Claude CLI subprocess timeout (seconds) |

## Further Reading

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture, state machines, concurrency model
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common problems and recovery procedures
