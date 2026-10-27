"""Smoke test: the package must be importable and expose a main()."""


def test_package_imports():
    import proton_mcp

    assert hasattr(proton_mcp, "__version__")


def test_server_main_exists():
    from proton_mcp.server import main

    assert callable(main)
