---
name: Test isolation issues
description: Known sources of inter-test pollution in kanban-agents
type: feedback
---

## store.py path constants

**Rule:** Always monkeypatch all four of store.DATA_DIR, store.FEATURES_DIR, store.TASKS_BASE,
store.ARTIFACTS_DIR. Setting KANBAN_DATA_DIR env var is insufficient.

**Why:** Constants are evaluated at module import time, before any test fixture runs.

**How to apply:** Use the `tmp_data_dir` fixture from conftest.py for any test touching the store.

## server._janky_mode global

**Rule:** Tests that assert on janky_mode state must reset it via monkeypatch at test start.

**Why:** It's a module-level bool that toggles in-place. test_client fixtures are per-test
but the server module is imported once per process, so the global leaks across tests.

**How to apply:** `monkeypatch.setattr(server, "_janky_mode", False)` in setUp or at test top.
