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
    elif settings.resolved_auth_mode() == "hybrid":
        checks.extend(
            [
                Check(
                    "auth-backend",
                    True,
                    "hybrid: official Personal API for wellness/raw data + email/password "
                    "for Garmin-derived metrics",
                ),
                Check(
                    "personal-api-key",
                    bool(settings.garmin_personal_api_key),
                    "GARMIN_PERSONAL_API_KEY configured"
                    if settings.garmin_personal_api_key
                    else "GARMIN_PERSONAL_API_KEY missing (hybrid needs both credential sets)",
                ),
                Check(
                    "garmin-email",
                    bool(settings.garmin_email),
                    "GARMIN_EMAIL configured (derived metrics)",
                ),
                Check(
                    "garmin-password",
                    bool(settings.garmin_password),
                    "GARMIN_PASSWORD configured (derived metrics)",
                ),
                Check(
                    "token-cache",
                    manager.token_cache_present(),
                    "Garmin token cache present"
                    if manager.token_cache_present()
                    else "Run garmin-mcp login",
                ),
                Check(
                    "personal-token-cache",
                    manager.personal_token_cache_present(),
                    f"Bearer token cache: {settings.garmin_personal_token_cache}"
                    if manager.personal_token_cache_present()
                    else "No cached bearer token yet (fetched on first request)",
                ),
            ]
        )
        if include_live:
            try:
                manager.login_hybrid()
                checks.append(
                    Check("garmin-live-login", True, "hybrid backend: both backends reachable")
                )
            except Exception as exc:
                checks.append(
                    Check("garmin-live-login", False, f"{type(exc).__name__}: {exc}")
                )
        include_live = False
    elif settings.resolved_auth_mode() == "personal":
        checks.extend(
            [
                Check(
                    "auth-backend",
                    True,
                    "official Garmin Personal API (GARMIN_AUTH_MODE=personal)",
                ),
                Check(
                    "personal-api-key",
                    bool(settings.garmin_personal_api_key),
                    "GARMIN_PERSONAL_API_KEY configured",
                ),
                Check(
                    "personal-token-cache",
                    manager.personal_token_cache_present(),
                    f"Bearer token cache: {settings.garmin_personal_token_cache}"
                    if manager.personal_token_cache_present()
                    else "No cached bearer token yet (fetched on first request)",
                ),
            ]
        )
        if include_live:
            try:
                manager.login_personal()
                checks.append(Check("personal-api-live", True, "Personal API token + profile OK"))
            except Exception as exc:
                checks.append(
                    Check("personal-api-live", False, f"{type(exc).__name__}: {exc}")
                )
        include_live = False
    else:
        checks.extend(
            [
                Check(
                    "auth-backend",
                    True,
                    "unofficial email/password flow (GARMIN_AUTH_MODE=unofficial)",
                ),
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
        if settings.garmin_personal_api_key:
            checks.append(
                Check(
                    "personal-api-key",
                    True,
                    "GARMIN_PERSONAL_API_KEY present but unused "
                    "(set GARMIN_AUTH_MODE=personal to switch)",
                )
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
