"""Shared fixtures for the kanban agent test suite."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """
    Redirect all store path constants to a temporary directory.

    store.py computes FEATURES_DIR, TASKS_BASE, and ARTIFACTS_DIR at import
    time from DATA_DIR, so they must be monkeypatched individually.  Setting
    KANBAN_DATA_DIR only affects code that reads the env var at call time (e.g.
    runner.py's DATA_DIR), not the already-evaluated module-level constants in
    store.py.
    """
    import controlplane.store as store

    data_dir = tmp_path / "data"
    features_dir = data_dir / "features"
    tasks_base = data_dir / "tasks"
    artifacts_dir = data_dir / "artifacts"

    monkeypatch.setenv("KANBAN_DATA_DIR", str(data_dir))
    monkeypatch.setattr(store, "DATA_DIR", data_dir)
    monkeypatch.setattr(store, "FEATURES_DIR", features_dir)
    monkeypatch.setattr(store, "TASKS_BASE", tasks_base)
    monkeypatch.setattr(store, "ARTIFACTS_DIR", artifacts_dir)

    # Create the full directory tree that _ensure_dirs() would create.
    store._ensure_dirs()

    yield data_dir


@pytest.fixture
def test_client(tmp_data_dir):
    """
    FastAPI TestClient with the supervisor loop replaced by a no-op coroutine.

    The lifespan handler calls asyncio.create_task(run_loop()), which would
    immediately try to poll for tasks and spawn threads.  We patch run_loop to
    prevent that.
    """
    async def _noop_run_loop():
        # Yield once so create_task has something to await on cancel.
        await asyncio.sleep(0)

    with patch("controlplane.supervisor.run_loop", side_effect=_noop_run_loop):
        from controlplane.server import app
        with TestClient(app) as client:
            yield client


@pytest.fixture
def sample_feature(tmp_data_dir):
    """A feature created directly via the store (bypasses the API)."""
    import controlplane.store as store

    feature, task = store.create_feature(
        title="Sample Feature",
        description="A test feature for unit tests",
    )
    return feature, task


@pytest.fixture
def sample_bug(tmp_data_dir):
    """A bug report created directly via the store."""
    import controlplane.store as store

    feature, task = store.create_bug_report(
        title="Sample Bug",
        description="A reproducible crash in production",
    )
    return feature, task
