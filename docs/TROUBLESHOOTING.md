# Troubleshooting

## Claude CLI Not Installed or Authenticated

The system requires the `claude` CLI to be installed and authenticated. If tasks
immediately fail with a non-zero exit code, check:

```bash
# Verify claude is on PATH
which claude

# Verify authentication
claude --print "hello" --output-format json
```

If the CLI is not found, install it per the
[Claude Code documentation](https://docs.anthropic.com/en/docs/claude-code).
If authentication has expired, run `claude` interactively to re-authenticate.

## Tasks Stuck in in_progress

The supervisor includes a watchdog that checks every poll cycle for stuck tasks.
A task is moved to `failed` if:

- Its Claude CLI process has died and the task has been in_progress for more
  than 30 seconds.
- Its process has been running longer than `KANBAN_STUCK_TIMEOUT` (default 300s).

If the watchdog has not caught a stuck task, you can kill it manually:

```bash
# Via the API
curl -X POST http://localhost:8000/api/tasks/TASK-XXXX/kill

# This sends SIGTERM, waits 2s, then SIGKILL if needed.
# The task is moved to failed.
```

You can also check the PID file directly:

```bash
cat data/logs/TASK-XXXX.pid
ps -p $(cat data/logs/TASK-XXXX.pid)
```

## Features Stuck in in_progress With No current_task

This typically happens when a merge fails and the supervisor cannot create the
next task. The feature has no active task, so the supervisor ignores it.

To recover, either:

1. Create a new task manually by hitting the approve endpoint again (if the
   feature was approved).
2. Abandon the feature and recreate it.

```bash
# Abandon
curl -X POST http://localhost:8000/api/features/FEATURE-XXXX/abandon
```

## Git Worktree Conflicts

Feature worktrees live at `repo/../.worktrees/FEATURE-XXXX`. If a worktree gets
into a broken state (e.g., interrupted rebase), the supervisor may fail to
dispatch tasks for that feature.

To recover:

```bash
# Remove the worktree manually
cd repo
git worktree remove --force ../.worktrees/FEATURE-XXXX
git worktree prune

# If the branch is also broken
git branch -D work/FEATURE-XXXX
```

The supervisor calls `cleanup_stale_worktrees` on startup, which prunes
orphaned worktree directories.

## Log File Locations

Each task produces a structured JSONL event log:

```
data/logs/TASK-XXXX.jsonl
```

Events include `start`, `tool_call`, `tool_result`, `text`, `result`, and
`error` entries with timestamps and elapsed times. The `result` event includes
`cost_usd`, `duration_ms`, and `num_turns`.

You can also stream events via the API:

```bash
curl http://localhost:8000/api/tasks/TASK-XXXX/events
```

## How to Retry Failed Tasks

Move a failed task back to ready via the API:

```bash
curl -X POST http://localhost:8000/api/tasks/TASK-XXXX/retry
```

The supervisor will pick it up on the next poll cycle. The task retains its
original configuration (agent type, description, iteration count).

## How to Reset State

To start fresh, stop the server and delete the data directory:

```bash
rm -rf data/
```

The server recreates the directory structure on startup. This does not affect
the target repository in `repo/` — only task and feature metadata is lost.

To also reset worktrees:

```bash
rm -rf repo/../.worktrees/
cd repo && git worktree prune
```

## Server Won't Start

**Port in use:**

```bash
# Find what's using the port
lsof -i :8000

# Use a different port
uv run uvicorn controlplane.server:app --port 8001
```

**Import errors:**

Ensure dependencies are installed:

```bash
uv sync
```

The project requires Python 3.12+. Check your version:

```bash
python --version
```

**Target repo not initialized:**

The `repo/` directory must be a valid git repository:

```bash
cd repo && git status
```

If it is not, initialize it:

```bash
cd repo && git init && git commit --allow-empty -m "init"
```
