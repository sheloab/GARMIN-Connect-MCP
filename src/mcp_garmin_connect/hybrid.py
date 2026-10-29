"""Hybrid backend: use the official Personal API *and* the unofficial flow together.

Neither backend alone covers everything a training agent wants:

* the official Personal API is stable and returns the raw ``.FIT``, SpO2, weight,
  hydration and steps, but it cannot see metrics Garmin derives on the activity
  (training status/load, readiness, VO2 Max, race predictions, personal records);
* the unofficial email/password flow does expose those derived metrics, but it is
  fragile and can be rate-limited or locked out by SSO changes.

``GARMIN_AUTH_MODE=hybrid`` runs both and routes each call to the backend that
actually owns the data, falling back to the other one when a call fails. That way
one process can serve a full snapshot without the caller knowing which system
answered.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any

from .errors import (
    PersonalApiAuthError,
    PersonalApiHttpError,
    PersonalApiRateLimitError,
    PersonalApiUnsupported,
)

log = logging.getLogger("mcp-garmin-connect")

# Record of "which backend answered which method" for the current capture scope.
# A ContextVar (not a plain global) because ``get_full_snapshot`` runs sections in
# parallel, and because the day-loop helpers fan out into a nested thread pool:
# child threads inherit the reference, so their calls land in the same dict.
_usage_var: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "garmin_usage_sources", default=None
)


@contextmanager
def capture_usage() -> Any:
    """Collect ``{method: backend}`` for calls made inside the block."""
    sources: dict[str, str] = {}
    token = _usage_var.set(sources)
    try:
        yield sources
    finally:
        _usage_var.reset(token)


def capture_call(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, dict[str, str]]:
    """Run ``fn`` and return ``(result, {method: backend})`` for the calls it made."""
    with capture_usage() as sources:
        result = fn(*args, **kwargs)
    return result, dict(sources)


def merge_usage(sources: dict[str, str]) -> None:
    """Fold known sources into the active capture scope (used for cache hits)."""
    current = _usage_var.get()
    if current is not None and sources:
        current.update(sources)


def _record(method: str, backend_name: str) -> None:
    sources = _usage_var.get()
    if sources is not None:
        sources[method] = backend_name



# Wellness and raw-recording reads: the official API owns these.
PERSONAL_METHODS = frozenset(
    {
        "get_sleep_data",
        "get_hrv_data",
        "get_rhr_day",
        "get_all_day_stress",
        "get_body_battery",
        "get_activities",
        "get_activity",
        "get_activity_splits",
        "get_activity_workouts",
        "get_body_composition",
        "get_daily_summaries",
        "get_spo2",
        "get_hydration",
        "get_steps",
        "get_fitness_age",
        "download_activity_file",
    }
)

# Garmin-derived metrics: only the unofficial flow has them.
UNOFFICIAL_METHODS = frozenset(
    {
        "get_training_status",
        "get_training_readiness",
        "get_race_predictions",
        "get_max_metrics",
        "get_cycling_ftp",
        "get_personal_record",
        "connectapi",
    }
)

# Errors that mean "this backend cannot answer", so the other one may try.
_FALLBACK_ERRORS = (PersonalApiUnsupported, PersonalApiHttpError, PersonalApiRateLimitError)
# Errors that mean "credentials are wrong" — never silently masked.
_FATAL_ERRORS = (PersonalApiAuthError,)


class UnavailableBackend:
    """Stands in for a backend that failed to initialise.

    Used when the Personal API key is expired/revoked: the hybrid keeps serving
    from the other backend instead of refusing to start, and calls that only the
    missing backend could answer raise a clear, recorded error.
    """

    def __init__(self, backend_name: str, reason: str) -> None:
        self.backend_name = backend_name
        self.reason = reason

    @property
    def display_name(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise PersonalApiUnsupported(f"{self.backend_name} unavailable: {self.reason}")

        return unavailable


class HybridBackend:
    """Routes each client call to the backend that owns that data set."""

    backend_name = "hybrid"

    def __init__(self, personal: Any, unofficial: Any) -> None:
        self._personal = personal
        self._unofficial = unofficial

    @property
    def supports_derived_metrics(self) -> bool:
        return any(
            getattr(backend, "supports_derived_metrics", True)
            for backend in (self._personal, self._unofficial)
        )

    @property
    def display_name(self) -> str | None:
        for backend in (self._personal, self._unofficial):
            name = getattr(backend, "display_name", None)
            if isinstance(name, str) and name:
                return name
        return None

    def close(self) -> None:
        for backend in (self._personal, self._unofficial):
            closer = getattr(backend, "close", None)
            if callable(closer):
                closer()

    def routing(self) -> dict[str, str]:
        """Which backend answers which method, for `doctor` and debugging."""
        return {
            "personal": sorted(PERSONAL_METHODS),
            "unofficial": sorted(UNOFFICIAL_METHODS),
        }

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        personal_fn = getattr(self._personal, name, None)
        unofficial_fn = getattr(self._unofficial, name, None)
        personal_name = getattr(self._personal, "backend_name", "personal")
        unofficial_name = getattr(self._unofficial, "backend_name", "unofficial")
        preferred = (
            [(personal_name, personal_fn), (unofficial_name, unofficial_fn)]
            if name in PERSONAL_METHODS
            else [(unofficial_name, unofficial_fn), (personal_name, personal_fn)]
        )
        available = [(backend, fn) for backend, fn in preferred if callable(fn)]
        if not available:
            raise AttributeError(name)
        return _make_router(name, available)


def _make_router(name: str, available: list[tuple[str, Any]]) -> Any:
    def call(*args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for index, (backend_name, fn) in enumerate(available):
            try:
                result = fn(*args, **kwargs)
            except _FATAL_ERRORS:
                raise
            except _FALLBACK_ERRORS as exc:
                last_error = exc
                if index == len(available) - 1:
                    break
                log.info("%s unavailable on one backend (%s), trying the other", name, exc)
            except Exception as exc:  # noqa: BLE001 - last resort fallback
                last_error = exc
                if index == len(available) - 1:
                    break
                log.info("%s failed (%s), trying the other backend", name, exc)
            else:
                _record(name, backend_name)
                return result
        if last_error is not None:
            raise last_error
        raise AttributeError(name)

    return call
