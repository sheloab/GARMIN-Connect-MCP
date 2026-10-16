import json

from mcp_garmin_connect.cli import main


def test_tools_command_lists_capabilities(capsys) -> None:
    main(["tools"])
    payload = json.loads(capsys.readouterr().out)
    assert "tools" in payload
    assert "prompts" in payload
    assert any(tool["name"] == "get_recovery" for tool in payload["tools"])


def test_ask_requires_external_health_data_opt_in() -> None:
    try:
        main(["ask", "How is my recovery?", "--provider", "deepseek"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("ask should require explicit external health data opt-in")
