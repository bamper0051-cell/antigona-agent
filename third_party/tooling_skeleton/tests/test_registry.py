from antigona.tools import build_default_registry


def test_default_registry_contains_core_tools():
    registry = build_default_registry()
    assert registry.names() == ("read_file", "run_pytest", "search_files", "terminal")
