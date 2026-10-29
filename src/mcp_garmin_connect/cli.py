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
        "--auth-mode",
        choices=["auto", "personal", "unofficial", "hybrid"],
        default=None,
        help="Override GARMIN_AUTH_MODE for this run (hybrid = both backends).",
    )
    serve.add_argument(
        "--demo",
        action="store_true",
        help="Serve fictional sample data instead of Garmin Connect (no credentials needed).",
    )

    doctor = sub.add_parser("doctor", help="Check local configuration.")
    doctor.add_argument("--live", action="store_true", help="Attempt a live Garmin login.")

    sub.add_parser("tools", help="List MCP tools and prompts.")

    personal = sub.add_parser(
        "personal-api",
        help="Validate the Garmin Personal API key (official backend) and probe endpoints.",
    )
    personal.add_argument(
        "--check",
        action="store_true",
        help="Validate the key, obtain a token, and fetch one sample from each core endpoint.",
    )
    personal.add_argument(
        "--refresh-token",
        action="store_true",
        help="Discard the cached bearer token and request a new one.",
    )
    personal.add_argument(
        "--probe",
        metavar="PATH",
        default="",
        help=(
            "GET one Personal API path and print the raw JSON "
            "(e.g. /userprofile-service/userprofile/settings)."
        ),
    )
    personal.add_argument(
        "--params",
        default="",
        metavar="k=v,k=v",
        help="Query parameters for --probe.",
    )

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
        if args.auth_mode:
            os.environ["GARMIN_AUTH_MODE"] = args.auth_mode
        run_server(transport=args.transport, host=args.host, port=args.port)
    elif args.command == "doctor":
        _doctor(include_live=args.live)
    elif args.command == "tools":
        print(json.dumps(list_capabilities(), indent=2, ensure_ascii=False))
    elif args.command == "personal-api":
        _personal_api(
            refresh_token=args.refresh_token,
            probe=args.probe,
            params=args.params,
        )
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


def _personal_api(*, refresh_token: bool, probe: str, params: str) -> None:
    """Validate the official Personal API key, then probe endpoints on request."""
    import time
    from datetime import date

    from .errors import PersonalApiError
    from .personal_api import PersonalApiAuthenticator

    settings = Settings.from_env()
    try:
        key = settings.require_personal_api_key()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    auth = PersonalApiAuthenticator(
        key,
        base_url=settings.garmin_personal_base_url,
        cache_path=settings.garmin_personal_token_cache,
        timeout=settings.garmin_personal_timeout_seconds,
    )
    try:
        token = auth.obtain_token(force_refresh=refresh_token)
    except PersonalApiError as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}") from exc
    expires = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(token.expires_at))
        if token.expires_at
        else "unknown (no exp claim)"
    )
    print(f"Personal API key: OK\ntoken expires: {expires}\ncache: {auth.cache_path}")

    from .personal_api import PersonalApiBackend, PersonalApiClient

    backend = PersonalApiBackend(PersonalApiClient(auth))

    if probe:
        query = {}
        for pair in params.split(","):
            if "=" in pair:
                name, value = pair.split("=", 1)
                query[name.strip()] = value.strip()
        print(json.dumps(backend._client.get(probe, query or None), indent=2, ensure_ascii=False))
        return

    today = date.today().isoformat()
    samples = {
        "profile": lambda: backend.get_user_profile(),
        "sleep": lambda: backend.get_sleep_data(today),
        "hrv": lambda: backend.get_hrv_data(today),
        "resting_heart_rate": lambda: backend.get_rhr_day(today),
        "stress": lambda: backend.get_all_day_stress(today),
        "body_battery": lambda: backend.get_body_battery(_shift(today, 7), today),
        "activities": lambda: backend.get_activities(0, 3),
        "weight": lambda: backend.get_body_composition(_shift(today, 7), today),
        "spo2": lambda: backend.get_spo2(today),
    }
    for name, fetch in samples.items():
        try:
            payload = fetch()
        except Exception as exc:  # noqa: BLE001 - report per endpoint, keep going
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            continue
        size = len(payload) if isinstance(payload, (list, dict)) else 1
        print(f"[OK]   {name}: {size} item(s)")
    print(
        "\nDerived metrics that the Personal API does not expose "
        "(training status/load, readiness, VO2 Max, race predictions, personal records) "
        "still require GARMIN_AUTH_MODE=unofficial."
    )


def _shift(day: str, days_back: int) -> str:
    from datetime import date as _date
    from datetime import timedelta

    return (_date.fromisoformat(day) - timedelta(days=days_back)).isoformat()


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
