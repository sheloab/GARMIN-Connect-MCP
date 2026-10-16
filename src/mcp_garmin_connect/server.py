from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .prompts import PROMPT_SPECS
from .tools import TOOL_SPECS


def create_server() -> FastMCP:
    mcp = FastMCP("mcp-garmin-connect")

    for spec in TOOL_SPECS:
        mcp.tool(name=spec.name, description=spec.description)(spec.function)

    for spec in PROMPT_SPECS:
        template = spec.template

        def prompt_fn(template: str = template) -> str:
            return template

        mcp.prompt(name=spec.name, description=spec.description)(prompt_fn)

    return mcp


def run_server(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    mcp = create_server()
    if transport == "http":
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
        return
    mcp.run(transport="stdio")


def list_capabilities() -> dict[str, list[dict[str, Any]]]:
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in TOOL_SPECS
        ],
        "prompts": [
            {
                "name": spec.name,
                "description": spec.description,
            }
            for spec in PROMPT_SPECS
        ],
    }
