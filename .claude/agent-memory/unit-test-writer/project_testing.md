---
name: Project testing setup
description: Testing framework, config, and fixture patterns for kanban-agents
type: project
---

- Framework: pytest 8.3.5 + pytest-asyncio 0.25.3 + httpx 0.28.1
- Config: `[tool.pytest.ini_options] testpaths = ["tests"]` in pyproject.toml
- Test files: tests/__init__.py, tests/conftest.py, tests/test_models.py, tests/test_store.py, tests/test_runner.py, tests/test_api.py

## Key fixture patterns

### tmp_data_dir
store.py has module-level path constants (FEATURES_DIR, TASKS_BASE, ARTIFACTS_DIR) evaluated
at import time from DATA_DIR. Setting the env var alone is not enough — all four constants
must be monkeypatched individually, then `store._ensure_dirs()` called to create subdirs.

### test_client
FastAPI lifespan calls `asyncio.create_task(run_loop())`. Patch `controlplane.supervisor.run_loop`
with an async no-op to prevent supervisor from polling during tests.

### _janky_mode isolation
`server._janky_mode` is a module-level global that persists across tests in the same process.
Tests that check toggle behavior must monkeypatch it to False at the start.
