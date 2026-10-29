from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import date, timedelta
from threading import BoundedSemaphore, Lock
from typing import Any

from .cache import TTLCache
from .config import DEFAULT_GARMIN_DEVICE, Settings
from .errors import GarminAuthError, GarminConnectionError, GarminRateLimitError

log = logging.getLogger("mcp-garmin-connect")

_TOKEN_CACHE_FILES = ("garmin_tokens.json", "oauth1_token.json", "oauth2_token.json")
_LOGIN_COOLDOWN_FILE = "login_cooldown.json"
_AUTH_ERROR_NAMES = {"GarminConnectAuthenticationError"}
_CONNECTION_ERROR_NAMES = {"GarminConnectConnectionError"}
_RATE_LIMIT_ERROR_NAMES = {"GarminConnectTooManyRequestsError"}
_PERSONAL_AUTH_ERROR_NAMES = {"PersonalApiAuthError"}
_PERSONAL_RATE_LIMIT_NAMES = {"PersonalApiRateLimitError"}


def apply_app_user_agent(version: str, device: str = DEFAULT_GARMIN_DEVICE) -> None:
    """Send the configured Garmin Connect Android app version on native API calls."""
    import importlib

    user_agent = f"GCM-Android-{version}"
    x_garmin_user_agent = (
        f"com.garmin.android.apps.connectmobile/{version}; ; {device}; Dalvik/2.1.0"
    )
    # garminconnect >= 0.3.19 reads these from client.transport (imported from
    # client.constants); older releases kept them in the client module itself.
    for name in ("garminconnect.client", "garminconnect.client.constants",
                 "garminconnect.client.transport"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "NATIVE_API_USER_AGENT"):
            module.NATIVE_API_USER_AGENT = user_agent
            module.NATIVE_X_GARMIN_USER_AGENT = x_garmin_user_agent


class GarminClientManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.cache = TTLCache(self.settings.cache_ttl_seconds)
        self._client: Any = None
        self._login_lock = Lock()
        self._request_semaphore = BoundedSemaphore(max(1, self.settings.garmin_max_concurrency))
        # Which backend filled each cached endpoint, so a cache hit can still be
        # attributed to a backend (hybrid mode reports per-section data_source).
        self._cache_sources: dict[str, dict[str, str]] = {}
        self._cache_sources_lock = Lock()
        # Hybrid mode can start with the email/password half unavailable; rebuild the
        # client once its login cooldown is over so that half gets another chance.
        self._rebuild_after: float | None = None

    @property
    def backend(self) -> str:
        """Active backend: ``demo``, ``personal`` (official API) or ``unofficial``."""
        return self.settings.resolved_auth_mode()

    @property
    def token_store(self) -> str:
        return self.settings.garmin_token_store

    def personal_token_cache_present(self) -> bool:
        return os.path.isfile(self.settings.garmin_personal_token_cache)

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

        apply_app_user_agent(self.settings.garmin_app_version, self.settings.garmin_device)
        email, password = self.settings.require_garmin_credentials()
        return Garmin(
            email=email,
            password=password,
            prompt_mfa=(lambda: input("MFA code: ")) if allow_interactive_mfa else None,
        )

    @property
    def login_cooldown_path(self) -> str:
        return os.path.join(self.token_store, _LOGIN_COOLDOWN_FILE)

    def login_cooldown(self) -> dict[str, Any] | None:
        """The active email/password login cooldown, shared by every server process."""
        try:
            with open(self.login_cooldown_path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or not isinstance(state.get("until"), (int, float)):
            return None
        return state if state["until"] > time.time() else None

    def _record_login_failure(self, exc: Exception) -> None:
        seconds = self.settings.garmin_login_cooldown_seconds
        if seconds <= 0:
            return
        state = {"until": time.time() + seconds, "reason": f"{type(exc).__name__}: {exc}"[:500]}
        try:
            os.makedirs(self.token_store, exist_ok=True)
            with open(self.login_cooldown_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
        except OSError as write_exc:
            log.warning("Could not save Garmin login cooldown: %s", write_exc)
        log.warning(
            "Garmin login failed; no new login attempts for %ss (%s)", seconds, state["reason"]
        )

    def clear_login_cooldown(self) -> None:
        if os.path.isfile(self.login_cooldown_path):
            os.remove(self.login_cooldown_path)

    def _raise_if_cooling_down(self) -> None:
        state = self.login_cooldown()
        if state is None:
            return
        until = time.strftime("%H:%M", time.localtime(state["until"]))
        raise GarminRateLimitError(
            f"Garmin login paused until {until} after a failed login ({state.get('reason')}). "
            "No request was sent to Garmin. Run `garmin-mcp login` after that time, or set "
            "GARMIN_LOGIN_COOLDOWN_SECONDS=0 to disable the pause."
        )

    def login(self, allow_interactive_mfa: bool = False) -> Any:
        # An interactive `garmin-mcp login` is a deliberate retry, so it skips the pause;
        # background tool calls must not keep hammering Garmin's SSO after a failure.
        if not allow_interactive_mfa:
            self._raise_if_cooling_down()
        try:
            client = self._login_once(allow_interactive_mfa)
        except Exception as exc:
            self._record_login_failure(exc)
            raise
        self.clear_login_cooldown()
        return client

    def _login_once(self, allow_interactive_mfa: bool) -> Any:
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

    def login_personal(self) -> Any:
        """Authenticate with the official Garmin Connect Personal API.

        The key is exchanged for a 24-hour bearer token; the token is cached next
        to the unofficial token store. A profile fetch validates the credentials
        before the first real tool call, so a bad/expired key fails loudly.
        """
        from .personal_api import PersonalApiAuthenticator, PersonalApiBackend, PersonalApiClient

        authenticator = PersonalApiAuthenticator(
            self.settings.require_personal_api_key(),
            base_url=self.settings.garmin_personal_base_url,
            cache_path=self.settings.garmin_personal_token_cache,
            timeout=self.settings.garmin_personal_timeout_seconds,
        )
        backend = PersonalApiBackend(
            PersonalApiClient(
                authenticator,
                max_concurrency=self.settings.garmin_personal_max_concurrency,
            ),
            batch_days=self.settings.garmin_personal_batch_days,
            batch_ttl_seconds=self.settings.garmin_personal_batch_ttl_seconds,
        )
        backend.get_user_profile()  # fail fast on an expired or revoked key
        log.info("Garmin Personal API authenticated (official backend)")
        return backend

    def login_hybrid(self) -> Any:
        """Run both backends and route each call to the one that owns the data.

        A broken Personal API key must not take the whole server down: someone
        whose key expired overnight should still get their derived metrics while
        they rotate it. The personal half is replaced by a stub that raises a
        recorded, explicit error, so shared reads fall back and personal-only
        tools say why.
        """
        from .hybrid import HybridBackend, UnavailableBackend

        try:
            personal: Any = self.login_personal()
        except Exception as exc:  # noqa: BLE001 - availability beats purity here
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("Personal API unavailable, continuing with email/password only: %s", reason)
            personal = UnavailableBackend("personal", reason)
        try:
            unofficial: Any = self.login(allow_interactive_mfa=False)
        except Exception as exc:
            if isinstance(personal, UnavailableBackend):
                raise
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "Email/password login unavailable, continuing with Personal API: %s", reason
            )
            unofficial = UnavailableBackend("unofficial", reason)
            state = self.login_cooldown()
            self._rebuild_after = state["until"] if state else time.time()
        else:
            self._rebuild_after = None
        log.info("Garmin hybrid backend active (official Personal API + email/password)")
        return HybridBackend(personal=personal, unofficial=unofficial)

    def client(self) -> Any:
        if self._client is not None and self._rebuild_after and time.time() >= self._rebuild_after:
            with self._login_lock:
                if self._rebuild_after and time.time() >= self._rebuild_after:
                    self._client = None
                    self._rebuild_after = None
        if self._client is None:
            with self._login_lock:
                if self._client is None:
                    mode = self.settings.resolved_auth_mode()
                    if mode == "demo":
                        from .demo import DemoGarmin

                        self._client = DemoGarmin()
                        log.info("Garmin demo mode active: fictional data, no credentials used")
                    elif mode == "personal":
                        self._client = self.login_personal()
                    elif mode == "hybrid":
                        self._client = self.login_hybrid()
                    else:
                        self._client = self.login(allow_interactive_mfa=False)
                        log.info(
                            "Garmin client authenticated with token cache: %s", self.token_store
                        )
        return self._client

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        return self.cache.get_or_set(key, factory)

    def remember_sources(self, key: str, sources: dict[str, str] | None) -> None:
        """Record which backend answered the calls behind one cache key."""
        if not sources:
            return
        with self._cache_sources_lock:
            if len(self._cache_sources) > 4096:
                self._cache_sources.clear()
            self._cache_sources[key] = dict(sources)

    def replay_sources(self, key: str) -> None:
        """Attribute a cache hit to the backend that originally filled it."""
        from .hybrid import merge_usage

        with self._cache_sources_lock:
            sources = self._cache_sources.get(key)
        if sources:
            merge_usage(sources)

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
            if name in _PERSONAL_AUTH_ERROR_NAMES | _PERSONAL_RATE_LIMIT_NAMES:
                raise
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
