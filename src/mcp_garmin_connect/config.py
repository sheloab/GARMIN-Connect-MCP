from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def load_environment() -> None:
    load_dotenv()
    for key in list(os.environ):
        if key.startswith("\ufeff"):
            os.environ.setdefault(key.lstrip("\ufeff"), os.environ[key])


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Device segment of the X-Garmin-User-Agent header ("maker/model/brand; Android/API").
# Override with GARMIN_DEVICE in .env to match your own phone.
DEFAULT_GARMIN_DEVICE = "samsung/SM-S928B/samsung; Android/35"


def _env_grid(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return min(1.0, max(0.001, value))


def _env_int_min(name: str, default: int, minimum: int = 1) -> int:
    return max(minimum, _env_int(name, default))


# Tool input caps. These used to be hardcoded in tools.py, which meant a personal
# deployment could not pull longer history without forking. Now env-tunable:
#   GARMIN_MAX_DAYS_HEALTH     sleep / RHR / stress / health_summary lookback (default 30)
#   GARMIN_MAX_DAYS_ACTIVITIES activity / load / running-summary lookback (default 365)
#   GARMIN_MAX_ACTIVITY_LIMIT  max activities returned per call (default 200)
# .env is loaded here because these are read at import time, before Settings exists.
load_environment()
MAX_DAYS_HEALTH = _env_int_min("GARMIN_MAX_DAYS_HEALTH", 30)
MAX_DAYS_ACTIVITIES = _env_int_min("GARMIN_MAX_DAYS_ACTIVITIES", 365)
MAX_ACTIVITY_LIMIT = _env_int_min("GARMIN_MAX_ACTIVITY_LIMIT", 200)


@dataclass(frozen=True)
class Settings:
    garmin_email: str | None
    garmin_password: str | None
    garmin_token_store: str
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    openai_api_key: str | None
    openai_base_url: str
    openai_model: str
    openrouter_api_key: str | None
    openrouter_base_url: str
    openrouter_model: str
    gemini_api_key: str | None
    gemini_base_url: str
    gemini_model: str
    cache_ttl_seconds: int
    garmin_max_concurrency: int = 3
    garmin_login_cooldown_seconds: int = 900
    garmin_device: str = DEFAULT_GARMIN_DEVICE
    air_quality_enabled: bool = True
    air_quality_grid_deg: float = 0.01
    garmin_demo: bool = False
    garmin_app_version: str = "5.30.1"
    garmin_personal_api_key: str | None = None
    garmin_auth_mode: str = "auto"
    garmin_personal_token_cache: str = "~/.garminconnect/personal_api_token.json"
    garmin_personal_base_url: str = "https://personal.connectapi.garmin.com"
    garmin_personal_timeout_seconds: float = 20.0
    garmin_personal_max_concurrency: int = 2
    garmin_personal_batch_days: int = 35
    garmin_personal_batch_ttl_seconds: int = 900

    @classmethod
    def from_env(cls) -> Settings:
        load_environment()
        return cls(
            garmin_email=os.getenv("GARMIN_EMAIL"),
            garmin_password=os.getenv("GARMIN_PASSWORD"),
            garmin_token_store=os.path.normpath(
                os.path.expanduser(os.getenv("GARMIN_TOKEN_STORE", "~/.garminconnect"))
            ),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL",
                "https://openrouter.ai/api/v1",
            ),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_base_url=os.getenv(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            cache_ttl_seconds=max(0, _env_int("GARMIN_CACHE_TTL_SECONDS", 120)),
            garmin_max_concurrency=max(1, _env_int("GARMIN_MAX_CONCURRENCY", 3)),
            garmin_login_cooldown_seconds=max(0, _env_int("GARMIN_LOGIN_COOLDOWN_SECONDS", 900)),
            garmin_device=os.getenv("GARMIN_DEVICE", "").strip() or DEFAULT_GARMIN_DEVICE,
            air_quality_enabled=os.getenv("AIR_QUALITY_ENABLED", "1").strip().lower()
            not in {"0", "false", "no", "off"},
            air_quality_grid_deg=_env_grid("AIR_QUALITY_GRID_DEG", 0.01),
            garmin_demo=_env_flag("GARMIN_DEMO"),
            garmin_app_version=os.getenv("GARMIN_APP_VERSION", "").strip() or "5.30.1",
            garmin_personal_api_key=os.getenv("GARMIN_PERSONAL_API_KEY") or None,
            garmin_auth_mode=os.getenv("GARMIN_AUTH_MODE", "auto").strip().lower() or "auto",
            garmin_personal_token_cache=os.path.normpath(
                os.path.expanduser(
                    os.getenv(
                        "GARMIN_PERSONAL_TOKEN_CACHE", "~/.garminconnect/personal_api_token.json"
                    )
                )
            ),
            garmin_personal_base_url=os.getenv(
                "GARMIN_PERSONAL_BASE_URL", "https://personal.connectapi.garmin.com"
            ),
            garmin_personal_timeout_seconds=float(
                os.getenv("GARMIN_PERSONAL_TIMEOUT_SECONDS", "20") or 20
            ),
            garmin_personal_max_concurrency=max(
                1, _env_int("GARMIN_PERSONAL_MAX_CONCURRENCY", 2)
            ),
            garmin_personal_batch_days=max(
                1, _env_int("GARMIN_PERSONAL_BATCH_DAYS", 35)
            ),
            garmin_personal_batch_ttl_seconds=max(
                0, _env_int("GARMIN_PERSONAL_BATCH_TTL_SECONDS", 900)
            ),
        )

    def require_personal_api_key(self) -> str:
        if not self.garmin_personal_api_key:
            raise RuntimeError(
                "GARMIN_PERSONAL_API_KEY is not set. Create a key at "
                "https://connect.garmin.com/app/settings/personalApiToken and put it in .env. "
                "See docs/personal-api.md."
            )
        return self.garmin_personal_api_key

    def resolved_auth_mode(self) -> str:
        """Which Garmin backend to use: ``personal``, ``unofficial``, ``hybrid`` or ``demo``.

        ``auto`` (the default) prefers the official Personal API when a key is
        configured, because it is stable and rate-limit friendly, and falls back
        to the email/password flow otherwise.

        ``hybrid`` needs both credential sets and routes each endpoint to the
        backend that owns it (see ``hybrid.py``), which is the only mode that
        returns derived metrics *and* raw files in one process.
        """
        if self.garmin_demo:
            return "demo"
        mode = (self.garmin_auth_mode or "auto").strip().lower()
        if mode in {"personal", "unofficial", "hybrid"}:
            return mode
        return "personal" if self.garmin_personal_api_key else "unofficial"

    def require_garmin_credentials(self) -> tuple[str, str]:
        if not self.garmin_email or not self.garmin_password:
            raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env or env vars.")
        return self.garmin_email, self.garmin_password

    def require_deepseek_key(self) -> str:
        return self.require_provider_key("deepseek")

    def require_provider_key(self, provider: str) -> str:
        provider_key = provider.lower()
        env_name_by_provider = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }
        key_by_provider = {
            "deepseek": self.deepseek_api_key,
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
            "gemini": self.gemini_api_key,
        }
        if provider_key not in key_by_provider:
            raise RuntimeError(f"Unsupported provider: {provider}")
        key = key_by_provider[provider_key]
        if not key:
            raise RuntimeError(f"{env_name_by_provider[provider_key]} must be set for {provider}.")
        return key
