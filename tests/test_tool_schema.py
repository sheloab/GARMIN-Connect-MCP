"""Contract tests: the MCP schemas clients see must match TOOL_SPECS metadata."""

from __future__ import annotations

import asyncio

from mcp_garmin_connect.server import create_server
from mcp_garmin_connect.tools import TOOL_SPECS


def test_registered_tool_schemas_match_tool_specs() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert set(tools) == {spec.name for spec in TOOL_SPECS}

    for spec in TOOL_SPECS:
        schema = tools[spec.name].inputSchema
        properties = schema.get("properties", {})
        for name, rule in spec.parameters.items():
            assert name in properties, f"{spec.name}.{name} missing from MCP schema"
            prop = properties[name]
            assert prop["type"] == rule["type"], f"{spec.name}.{name} type mismatch"
            for key in ("default", "minimum", "maximum"):
                if key in rule:
                    assert prop.get(key) == rule[key], f"{spec.name}.{name}.{key} mismatch"

        expected_required = sorted(
            name for name, rule in spec.parameters.items() if "default" not in rule
        )
        assert sorted(schema.get("required", [])) == expected_required, (
            f"{spec.name} required parameters mismatch"
        )


def test_tool_descriptions_are_present() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    for spec in TOOL_SPECS:
        assert tools[spec.name].description
