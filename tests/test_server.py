from mcp_garmin_connect.server import create_server, list_capabilities


def test_create_server() -> None:
    assert create_server() is not None


def test_list_capabilities() -> None:
    from mcp_garmin_connect.tools import TOOL_SPECS

    capabilities = list_capabilities()
    # Assert against the registry instead of a magic number, so adding a tool does
    # not require editing this test.
    assert len(capabilities["tools"]) == len(TOOL_SPECS)
    from mcp_garmin_connect.prompts import PROMPT_SPECS

    assert len(capabilities["prompts"]) == len(PROMPT_SPECS)
