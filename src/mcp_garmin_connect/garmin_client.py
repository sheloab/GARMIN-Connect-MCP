from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date, timedelta
from threading import BoundedSemaphore, Lock
from typing import Any

from .cache import TTLCache
from .config import Settings
from .errors import GarminAuthError, GarminConnectionError, GarminRateLimitError

log = logging.getLogger("mcp-garmin-connect")

_TOKEN_CACHE_FILES = ("garmin_tokens.json", "oauth1_token.json", "oauth2_token.json")
_AUTH_ERROR_NAMES = {"GarminConnectAuthenticationError"}
_CONNECTION_ERROR_NAMES = {"GarminConnectConnectionError"}
_RATE_LIMIT_ERROR_NAMES = {"GarminConnectTooManyRequestsError"}


class GarminClientManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.cache = TTLCache(self.settings.cache_ttl_seconds)
        self._client: Any = None
        self._login_lock = Lock()
        self._request_semaphore = BoundedSemaphore(max(1, self.settings.garmin_max_concurrency))

    @property
    def token_store(self) -> str:
        return self.settings.garmin_token_store

    def token_cache_present(self) -> bool:
        return any(
            os.path.isfile(os.path.join(self.token_store, name)) for name in _TOKEN_CACHE_FILES
        )

    def clear_token_cache(self) -> None:
        for name in _TOKEN_CACHE_FILES:
            path = os.path.join(self.token_store, name)
            if os.path.isfile(path):
                os.remove(path)
                log.info("Cleared stale Garmin token cache: %s", path)

    def build_client(self, allow_interactive_mfa: bool) -> Any:
        from garminconnect import Garmin

        email, password = self.settings.require_garmin_credentials()
        return Garmin(
            email=email,
            password=password,
            prompt_mfa=(lambda: input("MFA code: ")) if allow_interactive_mfa else None,
        )

    def login(self, allow_interactive_mfa: bool = False) -> Any:
        client = self.build_client(allow_interactive_mfa=allow_interactive_mfa)
        try:
            client.login(self.token_store)
            return client
        except Exception as exc:
            if type(exc).__name__ not in _AUTH_ERROR_NAMES or not self.token_cache_present():
                raise self._map_error(exc) from exc
            self.clear_token_cache()
            client = self.build_client(allow_interactive_mfa=allow_interactive_mfa)
            try:
                client.login(self.token_store)
            except Exception as retry_exc:
                raise self._map_error(retry_exc) from retry_exc
            return client

    def client(self) -> Any:
        if self._client is None:
            with self._login_lock:
                if self._client is None:
                    if self.settings.garmin_demo:
                        from .demo import DemoGarmin

                        self._client = DemoGarmin()
                        log.info("Garmin demo mode active: fictional data, no credentials used")
                    else:
                        self._client = self.login(allow_interactive_mfa=False)
                        log.info(
                            "Garmin client authenticated with token cache: %s", self.token_store
                        )
        return self._client

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        return self.cache.get_or_set(key, factory)

    def safe_call(
        self,
        fn: Callable[[], Any],
        default: Any = None,
        *,
        errors: list[dict[str, str]] | None = None,
        label: str | None = None,
    ) -> Any:
        """Run one Garmin endpoint, capping global concurrency.

        Auth/connection/rate-limit failures raise mapped package errors. Other
        endpoint failures are logged, recorded in ``errors`` when provided, and
        replaced with ``default`` so composite tools can report partial data
        instead of silently returning nulls.
        """
        try:
            with self._request_semaphore:
                return fn()
        except Exception as exc:
            name = type(exc).__name__
            if name in _AUTH_ERROR_NAMES | _CONNECTION_ERROR_NAMES | _RATE_LIMIT_ERROR_NAMES:
                raise self._map_error(exc) from exc
            source = label or name
            log.warning("Garmin endpoint failed (%s): %s", source, exc)
            if errors is not None:
                errors.append({"source": source, "error": f"{name}: {exc}"})
            return default

    def _map_error(self, exc: Exception) -> Exception:
        name = type(exc).__name__
        if name in _AUTH_ERROR_NAMES:
            return GarminAuthError(
                "Garmin authentication failed. Check GARMIN_EMAIL/GARMIN_PASSWORD and run "
                "`garmin-mcp login` from a real terminal."
            )
        if name in _RATE_LIMIT_ERROR_NAMES:
            return GarminRateLimitError("Garmin rate-limited the request. Wait a few minutes.")
        if name in _CONNECTION_ERROR_NAMES:
            return GarminConnectionError(f"Garmin connection failed: {exc}")
        return RuntimeError(f"Garmin request failed ({name}): {exc}")


_manager: GarminClientManager | None = None
_manager_lock = Lock()


def get_manager() -> GarminClientManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = GarminClientManager()
    return _manager


def reset_manager(settings: Settings | None = None) -> GarminClientManager:
    global _manager
    with _manager_lock:
        _manager = GarminClientManager(settings=settings)
    return _manager


def today_iso() -> str:
    return date.today().isoformat()


def date_range_iso(days: int) -> list[str]:
    safe_days = max(1, min(days, 365))
    return [(date.today() - timedelta(days=i)).isoformat() for i in range(safe_days)]
