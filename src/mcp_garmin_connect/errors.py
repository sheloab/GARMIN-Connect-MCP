from __future__ import annotations


class GarminMCPError(RuntimeError):
    """Base package error."""


class GarminAuthError(GarminMCPError):
    """Garmin credentials or token cache failed authentication."""


class GarminRateLimitError(GarminMCPError):
    """Garmin rejected a request because of rate limiting."""


class GarminConnectionError(GarminMCPError):
    """Garmin request failed because of a network or service issue."""


class PersonalApiError(GarminMCPError):
    """Base error for the official Garmin Connect Personal API."""


class PersonalApiAuthError(PersonalApiError):
    """The Personal API key is missing, expired, revoked, or rejected."""


class PersonalApiRateLimitError(PersonalApiError):
    """The Personal API rejected a request because of rate limiting."""


class PersonalApiHttpError(PersonalApiError):
    """The Personal API returned an unsuccessful response."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class PersonalApiUnsupported(PersonalApiError):
    """The requested metric is not exposed by the official Personal API.

    Garmin's Personal API covers the raw recording plus health summaries; values
    Garmin derives on top of an activity (VO2 Max, recovery time, performance
    condition, training status/load, race predictions) are not in the documented
    endpoint list.
    """
