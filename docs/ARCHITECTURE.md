# Architecture

## System Overview

Kanban Agents is a multi-agent orchestration system that uses Claude CLI instances
to plan, implement, review, and merge code changes autonomously. A FastAPI server
exposes a REST API and serves a browser UI. A background supervisor loop polls for
ready tasks and dispatches them to Claude CLI subprocesses running in isolated git
worktrees.

```
 Browser UI (index.html)
      |
      | HTTP
      v
 +-----------+        +--------------+
 |  FastAPI   |------->|  Supervisor  |  asyncio background task
 |  server.py |        |  supervisor  |  polls every KANBAN_POLL_INTERVAL seconds
 +-----------+        |  .py         |
      |                +------+-------+
      |                       |
      v                       | run_in_executor (ThreadPoolExecutor)
 +-----------+          +-----+------+
 |  Store     |<---------|  Runner    |  Claude CLI subprocesses
 |  store.py  |  results |  runner.py |  one per task, stream-json output
 +-----------+          +-----+------+
      |                       |
      v                       v
  data/                  repo/.worktrees/
  (YAML + JSON)          (per-feature git worktrees)
```

## Components

### Server (`controlplane/server.py`)

FastAPI application. Exposes REST endpoints for creating features and bug reports,
approving plans, responding to human tasks, killing/retrying tasks, and fetching
board state. Starts the supervisor loop as an asyncio background task via the
`lifespan` context manager. Resolves ticket references (FEATURE-XXXX, BUG-XXXX)
in descriptions before persisting them.

### Supervisor (`controlplane/supervisor.py`)

Async polling loop running inside the FastAPI process. Each cycle it:

1. Runs the watchdog to detect stuck or dead tasks.
2. Reaps finished futures from the `_running` dict.
3. Fetches ready tasks from the store.
4. Dispatches eligible tasks to the `ThreadPoolExecutor`.

Each agent type has a dedicated handler (`_handle_planner`, `_handle_triage`,
`_handle_code_worker`, `_handle_code_reviewer`, `_handle_freebase`) that runs
synchronously on a pool thread.

### Runner (`controlplane/runner.py`)

Invokes `claude --print --output-format stream-json` as a subprocess. Streams
structured events (tool calls, text, results) to a per-task JSONL log file.
Parses the final JSON result from the Claude output. Manages PID files for
process lifecycle tracking.

### Store (`controlplane/store.py`)

Filesystem-backed persistence. Features are stored as YAML files in
`data/features/`. Tasks are stored as YAML files in status-based subdirectories
(`data/tasks/{ready,in_progress,blocked,failed,done}/`). Artifacts (planner
results, triage results, worker results, review results) are stored as JSON
files in `data/artifacts/`.

### Worktree (`controlplane/worktree.py`)

Manages git worktrees for isolated agent execution. Two worktree types:

- **Feature worktrees** (`ensure_feature_worktree`): persistent, shared across
  all tasks for a feature. Located at `repo/../.worktrees/FEATURE-XXXX`. Branch
  name: `work/FEATURE-XXXX`. Removed when the feature is done.
- **Task worktrees** (`create_worktree`): temporary, used for freebase merges.
  Branch name: `task/TASK-XXXX`. Removed after the merge completes.

### Models (`controlplane/models.py`)

Plain dataclasses for Features, Tasks, and result types (WorkerResult,
ReviewResult, PlannerResult, TriageResult, FreebaseResult). No ORM. All
serialization goes through `dataclasses.asdict`.

## Data Flow

```
Feature created ──> Planner agent ──> Awaiting approval
                                          |
                              [approve]   |   [revise with feedback]
                                 |        └──> Planner (--continue)
                                 v
                          Code worker ──> Code reviewer
                                 ^              |
                                 |   [needs     |  [approved]
                                 |   changes]   v
                                 └─────────  Freebase merge ──> Done
```

Bug reports follow a similar flow but begin with a triage step instead of
planning. If triage finds the bug valid, it skips planning and goes directly
to a code worker.

## Feature Status State Machine

```
pending ──> planning ──> awaiting_approval ──> in_progress ──> merging ──> done
                ^              |                    ^
                └──────────────┘                    |
               (revise plan)            (review needs changes)

Bug-specific states:
pending ──> triaging ──> needs_info (park for user)
                 |            |
                 |            └──> triaging (resubmit)
                 └──> in_progress (valid bug, skip planning)
```

## Task Status State Machine

```
ready ──> in_progress ──> done
               |
               ├──> failed (timeout, crash, error)
               |       └──> ready (retry)
               └──> blocked (human task, awaiting response)
                       └──> done (responded)
```

## Agent Types

| Agent | Role | Tools Allowed |
|---|---|---|
| `planner` | Reads the codebase and produces an implementation plan | Bash, Read, Glob, Grep |
| `triage` | Evaluates bug reports for validity and reproducibility | Bash, Read, Glob, Grep |
| `code_worker` | Implements features or fixes based on plans/reviews | Bash, Read, Write, Edit, Glob, Grep |
| `code_reviewer` | Reviews worker commits against the plan and quality bar | Bash, Read, Glob, Grep |
| `freebase` | Resolves merge conflicts when rebasing onto the default branch | Bash, Read, Write, Edit, Glob, Grep |
| `human` | Represents a question directed at the user (never dispatched) | N/A |

## Concurrency Model

The supervisor runs on the asyncio event loop. Task execution is offloaded to a
dedicated `ThreadPoolExecutor` with `MAX_CONCURRENT` threads (default 5).
Additional limits:

- **Code workers**: at most `MAX_CONCURRENT_WORKERS` (default 3) running
  simultaneously.
- **Freebase merges**: serialized (max 1) to avoid race conditions on the
  default branch.
- **Human tasks**: never auto-dispatched; they sit in `blocked` until the user
  responds via the API.

The watchdog runs on the default executor (not the task pool) every poll cycle.
It kills tasks whose process has died (stale after 30s) or exceeded
`KANBAN_STUCK_TIMEOUT` (default 300s).

## Conversation Continuity

Code workers use `--continue` when invoking Claude CLI. All workers for the same
feature share a persistent worktree, so successive invocations (retries, review
iterations, human Q&A follow-ups) resume the previous conversation context. This
gives the agent memory across the full lifecycle of a feature.

## File Storage Layout

```
data/
  features/
    FEATURE-0001.yaml          # Feature metadata
    BUG-0001.yaml              # Bug report metadata
  tasks/
    ready/TASK-0001.yaml       # Queued tasks
    in_progress/TASK-0002.yaml # Running tasks
    blocked/TASK-0003.yaml     # Human tasks awaiting response
    failed/TASK-0004.yaml      # Failed tasks (retryable)
    done/TASK-0005.yaml        # Completed tasks
  artifacts/
    planner_results/TASK-0001.json
    triage_results/TASK-0002.json
    worker_results/TASK-0003.json
    review_results/TASK-0004.json
  logs/
    TASK-0001.jsonl             # Structured event log (stream-json)
    TASK-0001.pid               # PID file for running process
```
