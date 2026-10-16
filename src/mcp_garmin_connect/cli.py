from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Settings
from .doctor import run_checks
from .garmin_client import GarminClientManager
from .providers import create_agent, supported_providers
from .server import list_capabilities, run_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="garmin-mcp")
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Interactive Garmin login and token-cache setup.")

    serve = sub.add_parser("serve", help="Run the MCP server.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--demo",
        action="store_true",
        help="Serve fictional sample data instead of Garmin Connect (no credentials needed).",
    )

    doctor = sub.add_parser("doctor", help="Check local configuration.")
    doctor.add_argument("--live", action="store_true", help="Attempt a live Garmin login.")

    sub.add_parser("tools", help="List MCP tools and prompts.")

    ask = sub.add_parser("ask", help="Ask a provider-backed demo agent.")
    ask.add_argument("question")
    ask.add_argument("--provider", choices=supported_providers(), default="deepseek")
    ask.add_argument("--model", default=None)
    ask.add_argument("--max-tool-rounds", type=int, default=4)
    ask.add_argument(
        "--allow-external-health-data",
        action="store_true",
        help=(
            "Allow Garmin health/activity tool results to be sent to the selected LLM provider. "
            "Required for provider-backed tool calls."
        ),
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING))

    if args.command == "login":
        _login()
    elif args.command == "serve":
        if args.demo:
            os.environ["GARMIN_DEMO"] = "1"
        run_server(transport=args.transport, host=args.host, port=args.port)
    elif args.command == "doctor":
        _doctor(include_live=args.live)
    elif args.command == "tools":
        print(json.dumps(list_capabilities(), indent=2, ensure_ascii=False))
    elif args.command == "ask":
        _ask(
            args.question,
            provider=args.provider,
            model=args.model,
            max_tool_rounds=args.max_tool_rounds,
            allow_external_health_data=args.allow_external_health_data,
        )


def _login() -> None:
    settings = Settings.from_env()
    manager = GarminClientManager(settings=settings)
    print("Garmin Connect login...", file=sys.stderr)
    manager.login(allow_interactive_mfa=True)
    print(f"OK. Tokens saved in {manager.token_store}", file=sys.stderr)


def _doctor(include_live: bool) -> None:
    checks = run_checks(include_live=include_live)
    failed = False
    for check in checks:
        status = "OK" if check.ok else "WARN"
        optional = {
            "token-store",
            "token-cache",
            "deepseek-key",
            "openai-key",
            "openrouter-key",
            "gemini-key",
        }
        if not check.ok and check.name not in optional:
            failed = True
            status = "FAIL"
        print(f"[{status}] {check.name}: {check.detail}")
    if failed:
        raise SystemExit(1)


def _ask(
    question: str,
    provider: str,
    model: str | None,
    max_tool_rounds: int,
    allow_external_health_data: bool,
) -> None:
    if provider != "deepseek":
        raise SystemExit(f"Unsupported provider: {provider}")
    if not allow_external_health_data:
        raise SystemExit(
            "Refusing to send Garmin health/activity data to an external LLM provider without "
            "explicit opt-in. Re-run with --allow-external-health-data after you understand "
            f"that Garmin tool results may be sent to {provider}."
        )
    result = create_agent(provider, model=model).ask(
        question,
        max_tool_rounds=max_tool_rounds,
        allow_external_health_data=True,
    )
    if result.tool_calls:
        print(f"Tool calls: {', '.join(result.tool_calls)}", file=sys.stderr)
    print(result.answer)
