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
    garmin_max_concurrency: int = 6
    garmin_demo: bool = False

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
            garmin_max_concurrency=max(1, _env_int("GARMIN_MAX_CONCURRENCY", 6)),
            garmin_demo=_env_flag("GARMIN_DEMO"),
        )

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
