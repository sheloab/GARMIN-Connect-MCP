from __future__ import annotations

from typing import Any

import pytest

from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.providers import (
    GeminiAgent,
    OpenAIAgent,
    OpenRouterAgent,
    create_agent,
    gemini_tools,
    openai_compatible_tools,
    provider_specs,
)


def settings() -> Settings:
    return Settings(
        garmin_email="x",
        garmin_password="y",
        garmin_token_store=".",
        deepseek_api_key="deepseek-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-pro",
        openai_api_key="openai-key",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-5",
        openrouter_api_key="openrouter-key",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="google/gemini-3-flash-preview",
        gemini_api_key="gemini-key",
        gemini_base_url="https://generativelanguage.googleapis.com/v1beta",
        gemini_model="gemini-3.6-flash",
        cache_ttl_seconds=60,
    )


def test_provider_registry_and_defaults() -> None:
    specs = provider_specs(settings())
    assert specs["deepseek"].default_model == "deepseek-v4-pro"
    assert specs["openai"].default_model == "gpt-5"
    assert specs["openrouter"].default_model == "google/gemini-3-flash-preview"
    assert specs["gemini"].default_model == "gemini-3.6-flash"
    assert isinstance(create_agent("openai", settings=settings()), OpenAIAgent)
    assert isinstance(create_agent("openrouter", settings=settings()), OpenRouterAgent)
    assert isinstance(create_agent("gemini", settings=settings()), GeminiAgent)


def test_provider_requires_health_data_opt_in() -> None:
    agent = create_agent("openai", settings=settings())
    with pytest.raises(PermissionError):
        agent.ask("How is my recovery?")


def test_tool_payload_shapes() -> None:
    openai_tool = openai_compatible_tools()[0]
    gemini_tool = gemini_tools()[0]
    assert openai_tool["type"] == "function"
    assert "parameters" in openai_tool["function"]
    assert "function_declarations" in gemini_tool


def test_openai_compatible_tool_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "function": {
                                            "name": "get_sleep",
                                            "arguments": '{"days": 1}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "done"}}]}

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, *args: Any, **kwargs: Any) -> Response:
            calls.append(kwargs["json"])
            return Response()

    monkeypatch.setattr("mcp_garmin_connect.providers.httpx.Client", Client)
    monkeypatch.setattr(
        "mcp_garmin_connect.providers.call_tool",
        lambda name, args: {"ok": True, "name": name, "args": args},
    )

    result = create_agent("openai", settings=settings()).ask(
        "test",
        allow_external_health_data=True,
    )

    assert result.answer == "done"
    assert result.tool_calls == ["get_sleep"]
    assert calls[0]["tools"][0]["type"] == "function"


def test_gemini_tool_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            if len(calls) == 1:
                return {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_sleep",
                                            "args": {"days": 1},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            return {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, *args: Any, **kwargs: Any) -> Response:
            calls.append(kwargs["json"])
            return Response()

    monkeypatch.setattr("mcp_garmin_connect.providers.httpx.Client", Client)
    monkeypatch.setattr(
        "mcp_garmin_connect.providers.call_tool",
        lambda name, args: {"ok": True, "name": name, "args": args},
    )

    result = create_agent("gemini", settings=settings()).ask(
        "test",
        allow_external_health_data=True,
    )

    assert result.answer == "done"
    assert result.tool_calls == ["get_sleep"]
    assert "function_declarations" in calls[0]["tools"][0]
