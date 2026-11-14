"""Smoke test: the package must be importable and expose the three tools."""


def test_package_imports():
    import strava_mcp

    assert hasattr(strava_mcp, "__version__")


def test_server_main_exists():
    from strava_mcp.server import main

    assert callable(main)


def test_three_tools_registered():
    """D6 + the local-store tool (D9): four registered tools, no extras."""
    from strava_mcp.server import INPUT_SCHEMAS, TOOL_SPECS

    assert set(TOOL_SPECS) == {
        "strava_athlete",
        "strava_activities",
        "strava_rate_status",
        "strava_sync",
    }
    assert set(INPUT_SCHEMAS) == set(TOOL_SPECS)


def test_schema_actions_cannot_drift():
    """Schemas are built from the tool tables (section 3.2)."""
    from strava_mcp.server import INPUT_SCHEMAS, TOOL_SPECS

    for name, schema in INPUT_SCHEMAS.items():
        actions = schema["properties"]["action"]["enum"]
        assert list(actions) == list(TOOL_SPECS[name]["tool"].ACTIONS)
        # v1 is read-only: no gating anywhere (F1).
        assert "confirm" not in schema["properties"]
