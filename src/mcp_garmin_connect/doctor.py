from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass

from .config import Settings
from .garmin_client import GarminClientManager
from .providers import provider_specs


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(include_live: bool = False) -> list[Check]:
    settings = Settings.from_env()
    manager = GarminClientManager(settings=settings)
    checks = [
        Check("python-package", True, "mcp-garmin-connect import OK"),
        Check("mcp", importlib.util.find_spec("mcp") is not None, "Python MCP SDK installed"),
        Check(
            "garminconnect",
            importlib.util.find_spec("garminconnect") is not None,
            "garminconnect installed",
        ),
    ]
    if settings.garmin_demo:
        checks.append(
            Check("demo-mode", True, "GARMIN_DEMO=1: serving fictional sample data")
        )
        include_live = False
    else:
        checks.extend(
            [
                Check("garmin-email", bool(settings.garmin_email), "GARMIN_EMAIL configured"),
                Check(
                    "garmin-password",
                    bool(settings.garmin_password),
                    "GARMIN_PASSWORD configured",
                ),
                Check(
                    "token-store",
                    os.path.isdir(settings.garmin_token_store),
                    f"Token store: {settings.garmin_token_store}",
                ),
                Check(
                    "token-cache",
                    manager.token_cache_present(),
                    "Garmin token cache present"
                    if manager.token_cache_present()
                    else "Run garmin-mcp login",
                ),
            ]
        )
    for provider_name, spec in provider_specs(settings).items():
        checks.append(
            Check(
                f"{provider_name}-key",
                spec.api_key_configured,
                f"{provider_name} API key configured"
                if spec.api_key_configured
                else f"Optional {provider_name} API key missing",
            )
        )
    if include_live:
        try:
            manager.login(allow_interactive_mfa=False)
            checks.append(Check("garmin-live-login", True, "Garmin login OK"))
        except Exception as exc:
            checks.append(Check("garmin-live-login", False, f"{type(exc).__name__}: {exc}"))
    return checks
