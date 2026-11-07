"""Shared fixtures for the strava_mcp tests (house rules: no network).

The audit layer is redirected into tmp_path (so tests never write to the
runner's real home), the rate tracker starts fresh per test, and no HTTP
transport env vars leak between tests.
"""

from __future__ import annotations

import pytest

from strava_mcp import audit, rate


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_BEARER", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(audit, "_LOGS_DIR", str(tmp_path / "mcp-logs"))
    monkeypatch.setattr(rate, "STATE", rate.RateState())
