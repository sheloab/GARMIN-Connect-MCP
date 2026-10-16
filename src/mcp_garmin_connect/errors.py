from __future__ import annotations


class GarminMCPError(RuntimeError):
    """Base package error."""


class GarminAuthError(GarminMCPError):
    """Garmin credentials or token cache failed authentication."""


class GarminRateLimitError(GarminMCPError):
    """Garmin rejected a request because of rate limiting."""


class GarminConnectionError(GarminMCPError):
    """Garmin request failed because of a network or service issue."""
