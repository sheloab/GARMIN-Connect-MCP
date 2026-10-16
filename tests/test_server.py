from mcp_garmin_connect.server import create_server, list_capabilities


def test_create_server() -> None:
    assert create_server() is not None


def test_list_capabilities() -> None:
    capabilities = list_capabilities()
    assert len(capabilities["tools"]) == 36
    assert len(capabilities["prompts"]) == 4
