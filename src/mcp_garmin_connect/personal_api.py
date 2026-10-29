"""Garmin Connect **Personal API** backend (official, self-serve).

Garmin opened a personal tier of the Connect Developer Program in October 2026:
you create a key yourself at
https://connect.garmin.com/app/settings/personalApiToken, exchange it for a
24-hour OAuth2 bearer token, and read your own data. No application, no
business entity, no approval queue.

Compared with the email/password flow used by ``garminconnect`` this backend
trades breadth for stability: it is rate-limit friendly and does not fight
Cloudflare/SSO, but it only exposes the documented endpoints. Metrics Garmin
derives on top of an activity (VO2 Max, recovery time, performance condition,
training status/load, race predictions) are **not** in that list, so
``PersonalApiBackend`` raises :class:`PersonalApiUnsupported` for them and the
composite tools degrade to ``None`` plus an ``errors`` entry.

Docs: https://developer.garmin.com/gc-developer-program/personal/personal-api/
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from threading import BoundedSemaphore, Lock
from typing import Any

import httpx

from .errors import (
    PersonalApiAuthError,
    PersonalApiHttpError,
    PersonalApiRateLimitError,
    PersonalApiUnsupported,
)

log = logging.getLogger("mcp-garmin-connect")

DEFAULT_BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
DEFAULT_TOKEN_CACHE = "~/.garminconnect/personal_api_token.json"

# Refresh this long before the bearer token expires so a long tool call never
# dies half way through on a token that lapsed mid-request.
TOKEN_REFRESH_MARGIN_SECONDS = 3600


def jwt_expiry(token: str) -> int | None:
    """Return the ``exp`` claim (epoch seconds) of a JWT, or None if unreadable."""
    try:
        payload = token.split(".")[1]
    except (AttributeError, IndexError):
        return None
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    exp = decoded.get("exp") if isinstance(decoded, dict) else None
    return int(exp) if isinstance(exp, (int, float)) else None


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class BearerToken:
    access_token: str
    expires_at: int | None = None

    def is_fresh(self, margin_seconds: int = TOKEN_REFRESH_MARGIN_SECONDS) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at - margin_seconds > int(time.time())


class PersonalApiAuthenticator:
    """Exchanges a Personal API key for a bearer token and caches it.

    The cache is keyed by a hash of the key so rotating the key cannot serve a
    stale token. Tokens are valid for 24 hours and Garmin does not issue refresh
    tokens, so "refreshing" simply means asking for a new one.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cache_path: str = DEFAULT_TOKEN_CACHE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise PersonalApiAuthError(
                "GARMIN_PERSONAL_API_KEY is empty. Create a key at "
                "https://connect.garmin.com/app/settings/personalApiToken"
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.cache_path = os.path.expanduser(cache_path)
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> PersonalApiAuthenticator:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
            self._owns_client = True
        return self._client

    # -- token cache -------------------------------------------------------
    def _read_cache(self) -> BearerToken | None:
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("api_key_fingerprint") != _key_fingerprint(self.api_key):
            return None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        expires_at = payload.get("expires_at")
        return BearerToken(
            access_token=token,
            expires_at=int(expires_at) if isinstance(expires_at, (int, float)) else None,
        )

    def _write_cache(self, token: BearerToken) -> None:
        payload = {
            "api_key_fingerprint": _key_fingerprint(self.api_key),
            "access_token": token.access_token,
            "expires_at": token.expires_at,
            "cached_at": int(time.time()),
        }
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            fd = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except OSError as exc:  # cache is an optimisation, never a hard failure
            log.warning("Could not cache Personal API token at %s: %s", self.cache_path, exc)

    def clear_cache(self) -> None:
        try:
            os.remove(self.cache_path)
        except OSError:
            return

    # -- token exchange ----------------------------------------------------
    def obtain_token(self, force_refresh: bool = False) -> BearerToken:
        if not force_refresh:
            cached = self._read_cache()
            if cached is not None and cached.is_fresh():
                return cached

        try:
            response = self.http.post(
                f"{self.base_url}{TOKEN_PATH}",
                headers={"Authorization": f"ApiKey {self.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise PersonalApiHttpError(f"Personal API token request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise PersonalApiAuthError(
                "Garmin rejected the Personal API key "
                f"(HTTP {response.status_code}). Keys expire after 30/90/180/365 days — "
                "create a new one at https://connect.garmin.com/app/settings/personalApiToken"
            )
        if response.status_code == 429:
            raise PersonalApiRateLimitError(
                "Garmin rate-limited the Personal API token endpoint. Wait a few minutes."
            )
        if response.status_code >= 400:
            raise PersonalApiHttpError(
                f"Personal API token request failed with HTTP {response.status_code}",
                status_code=response.status_code,
                url=str(response.request.url),
            )

        token = _token_from_response(response)
        if not token:
            raise PersonalApiHttpError(
                "Personal API token response contained no recognisable access token",
                status_code=response.status_code,
                url=str(response.request.url),
            )

        bearer = BearerToken(access_token=token, expires_at=jwt_expiry(token))
        self._write_cache(bearer)
        return bearer


def _extract_access_token(body: Any) -> str | None:
    if isinstance(body, str):
        return _plausible_token(body)
    if not isinstance(body, dict):
        return None
    for key in ("access_token", "accessToken", "token", "id_token"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return _plausible_token(value)
    return None


_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=:\-]{20,8192}$")


def _plausible_token(candidate: str) -> str | None:
    """Accept a token only if it looks like one (base64url/JWT charset, no HTML)."""
    token = candidate.strip().strip('"').strip()
    return token if _TOKEN_SHAPE.match(token) else None


def _token_from_response(response: Any) -> str | None:
    """Pull the access token out of the token response.

    Garmin's documented exchange returns the bearer token as a **bare string**
    with ``content-type: text/plain`` (verified 2026-10-28), while other accounts
    and older behaviour use a JSON body like ``{"access_token": "..."}``. Accept
    both instead of assuming JSON — that assumption cost a round trip here.
    """
    content_type = (response.headers.get("content-type") or "").lower()
    text = (response.text or "").strip()

    if "json" in content_type or text.startswith("{") or text.startswith('"'):
        try:
            return _extract_access_token(response.json())
        except ValueError:
            pass  # fall through to the plain-text interpretation
    return _plausible_token(text)


class PersonalApiClient:
    """Thin authenticated GET client for the Personal API host.

    Garmin rate-limits this tier hard: a burst of a dozen parallel requests comes
    back as ``429`` with ``Retry-After`` (measured 2026-10-28). So the client caps
    its own concurrency and honours ``Retry-After`` instead of hammering, which
    also keeps a full snapshot from burning the budget in one go.
    """

    MAX_429_RETRIES = 2

    def __init__(
        self,
        authenticator: PersonalApiAuthenticator,
        *,
        max_concurrency: int = 2,
    ) -> None:
        self.authenticator = authenticator
        self._semaphore = BoundedSemaphore(max(1, max_concurrency))

    def close(self) -> None:
        self.authenticator.close()

    def __enter__(self) -> PersonalApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(
        self, path: str, params: dict[str, Any] | None
    ) -> httpx.Response:
        with self._semaphore:
            token = self.authenticator.obtain_token()
            url = f"{self.authenticator.base_url}{path}"
            return self.authenticator.http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token.access_token}"},
            )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("retry-after")
        try:
            return min(30.0, max(0.0, float(raw))) if raw is not None else 2.0 * (attempt + 1)
        except ValueError:
            return 2.0 * (attempt + 1)

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        _retry_on_401: bool = True,
    ) -> Any:
        url = f"{self.authenticator.base_url}{path}"
        for attempt in range(self.MAX_429_RETRIES + 1):
            try:
                response = self._request(path, params)
            except httpx.HTTPError as exc:
                raise PersonalApiHttpError(
                    f"Personal API request failed: {exc}", url=url
                ) from exc
            if response.status_code == 429 and attempt < self.MAX_429_RETRIES:
                wait = self._retry_after_seconds(response, attempt)
                log.info("Personal API 429 on %s, retrying in %.0fs", path, wait)
                time.sleep(wait)
                continue
            break

        if response.status_code == 401 and _retry_on_401:
            # A token can be revoked before its exp claim; ask for a new one once.
            log.info("Personal API returned 401, refreshing bearer token")
            self.authenticator.obtain_token(force_refresh=True)
            return self.get(path, params, _retry_on_401=False)
        if response.status_code == 401:
            raise PersonalApiAuthError(
                "Personal API rejected the bearer token. Run "
                "`garmin-mcp personal-api --refresh-token` or delete the token cache."
            )
        if response.status_code == 429:
            raise PersonalApiRateLimitError(
                "Garmin rate-limited the Personal API. Wait a few minutes and retry."
            )
        if response.status_code >= 400:
            raise PersonalApiHttpError(
                f"Personal API request failed with HTTP {response.status_code}",
                status_code=response.status_code,
                url=str(response.request.url),
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        token = self.authenticator.obtain_token()
        url = f"{self.authenticator.base_url}{path}"
        try:
            response = self.authenticator.http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise PersonalApiHttpError(f"Personal API download failed: {exc}", url=url) from exc
        if response.status_code == 401:
            self.authenticator.obtain_token(force_refresh=True)
            token = self.authenticator.obtain_token()
            try:
                response = self.authenticator.http.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token.access_token}"},
                )
            except httpx.HTTPError as exc:
                raise PersonalApiHttpError(
                    f"Personal API download failed: {exc}", url=url
                ) from exc
        if response.status_code >= 400:
            raise PersonalApiHttpError(
                f"Personal API download failed with HTTP {response.status_code}",
                status_code=response.status_code,
                url=str(response.request.url),
            )
        return response.content


def _first(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
        return {}
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return _first(payload["data"])
    return payload if isinstance(payload, dict) else {}


def _flatten_values(item: dict[str, Any]) -> dict[str, Any]:
    """The stress stats endpoint nests its numbers under ``values``."""
    values = item.get("values")
    if isinstance(values, dict):
        merged = {key: value for key, value in item.items() if key != "values"}
        merged.update(values)
        return merged
    return item


CADENCE_KEYS = ("directDoubleCadence", "directRunCadence", "directBikeCadence")
ELEVATION_DEADBAND_M = 0.5


def _metric_index(payload: dict[str, Any]) -> dict[str, int]:
    descriptors = payload.get("metricDescriptors")
    index: dict[str, int] = {}
    if isinstance(descriptors, list):
        for position, descriptor in enumerate(descriptors):
            if isinstance(descriptor, dict):
                key = descriptor.get("key")
                if isinstance(key, str):
                    index[key] = position
    return index


def laps_from_details(
    payload: dict[str, Any], lap_distance_m: float = 1000.0
) -> list[dict[str, Any]]:
    """Rebuild per-km laps from Garmin's per-sample activity stream.

    Samples carry cumulative distance/time plus instantaneous HR, cadence and
    elevation. Lap boundaries are interpolated between samples, so pace is not
    quantised to the 1-2 s sample interval.
    """
    rows = payload.get("activityDetailMetrics")
    index = _metric_index(payload)
    if not isinstance(rows, list) or not rows or "sumDistance" not in index:
        return []

    def value(row: dict[str, Any], key: str) -> float | None:
        position = index.get(key)
        metrics = row.get("metrics")
        if position is None or not isinstance(metrics, list) or position >= len(metrics):
            return None
        raw = metrics[position]
        return float(raw) if isinstance(raw, (int, float)) else None

    samples: list[dict[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        distance = value(row, "sumDistance")
        if distance is None:
            continue
        sample = {"distance": distance, "time": value(row, "sumDuration")}
        for key in ("directHeartRate", *CADENCE_KEYS, "directElevation"):
            sample[key] = value(row, key)
        samples.append(sample)
    if len(samples) < 2:
        return []

    total_distance = samples[-1]["distance"]
    if not total_distance or total_distance <= 0:
        return []

    def time_at(distance: float) -> float | None:
        first_time, last_time = samples[0]["time"], samples[-1]["time"]
        if distance <= samples[0]["distance"]:
            return first_time
        if distance >= samples[-1]["distance"]:
            return last_time
        for previous, current in zip(samples, samples[1:], strict=False):
            start, end = previous["distance"], current["distance"]
            if start <= distance <= end and end > start:
                t0, t1 = previous["time"], current["time"]
                if t0 is None or t1 is None:
                    return None
                return t0 + (distance - start) / (end - start) * (t1 - t0)
        return last_time

    laps: list[dict[str, Any]] = []
    full_laps = int(total_distance // lap_distance_m)
    boundaries = [boundary * lap_distance_m for boundary in range(0, full_laps + 1)]
    boundaries.append(total_distance)
    for position, (start_m, end_m) in enumerate(
        zip(boundaries, boundaries[1:], strict=False)
    ):
        if end_m - start_m < 1:
            continue
        is_final_lap = position == len(boundaries) - 2

        def in_lap(
            distance: float,
            start_m: float = start_m,
            end_m: float = end_m,
            is_final: bool = is_final_lap,
        ) -> bool:
            # The last sample sits exactly on the final boundary; it belongs here.
            return start_m <= distance < end_m or (is_final and distance == end_m)

        start_time, end_time = time_at(start_m), time_at(end_m)
        duration = (
            end_time - start_time if start_time is not None and end_time is not None else None
        )
        hearts = [
            sample["directHeartRate"]
            for sample in samples
            if sample["directHeartRate"] is not None and in_lap(sample["distance"])
        ]
        cadences = [
            next(
                (sample[key] for key in CADENCE_KEYS if sample.get(key) is not None),
                None,
            )
            for sample in samples
            if in_lap(sample["distance"])
        ]
        cadences = [value for value in cadences if value is not None]
        elevations = [
            sample["directElevation"]
            for sample in samples
            if sample["directElevation"] is not None and start_m <= sample["distance"] <= end_m
        ]
        # Elevation samples are noisy at 1-2 s granularity; without a deadband the
        # positive deltas inflate wildly (this 7 km run summed to 24 m vs Garmin's
        # 11 m). Count a climb only once it clears the deadband.
        gain = 0.0
        reference = elevations[0] if elevations else None
        for elevation in elevations[1:]:
            if reference is None:
                reference = elevation
                continue
            delta = elevation - reference
            if abs(delta) >= ELEVATION_DEADBAND_M:
                if delta > 0:
                    gain += delta
                reference = elevation
        lap: dict[str, Any] = {
            "distance": end_m - start_m,
            "duration": duration,
            "averageHR": round(sum(hearts) / len(hearts)) if hearts else None,
            "maxHR": round(max(hearts)) if hearts else None,
            "averageRunCadence": round(sum(cadences) / len(cadences), 1) if cadences else None,
            "elevationGain": round(gain, 1) if elevations else None,
        }
        if duration and duration > 0:
            lap["averageSpeed"] = (end_m - start_m) / duration
        laps.append(lap)
    return laps


class PersonalApiBackend:
    """Adapter that gives the Personal API the same surface the tools already use.

    Method names and return shapes mirror the ``garminconnect`` client so
    ``tools.py`` does not need a second code path. Shapes are the same DTOs the
    Garmin web app consumes, with light wrapping where the official endpoint
    returns the inner object instead of the outer envelope.
    """

    backend_name = "personal"
    #: Garmin-derived metrics (VO2 Max, readiness, training status, race
    #: predictions, PRs) are not in this tier; callers can skip instead of
    #: probing every date and collecting identical errors.
    supports_derived_metrics = False

    #: Per-day tools ask for one day at a time; the API charges per request and
    #: rate-limits hard, so a rolling window is fetched once and served from
    #: memory. Garmin caps how long a range may be, per endpoint (measured
    #: 2026-10-28: sleep 31 days OK / 35 rejected, stress 28 OK / 31 rejected,
    #: dailysummaries and HRV 35 OK). Stay inside the verified maximums.
    DEFAULT_BATCH_DAYS = 28
    DEFAULT_BATCH_TTL_SECONDS = 900
    RANGE_LIMITS = {"sleep": 30, "stress": 28, "summaries": 30, "hrv": 30}

    def __init__(
        self,
        client: PersonalApiClient,
        *,
        batch_days: int = DEFAULT_BATCH_DAYS,
        batch_ttl_seconds: int = DEFAULT_BATCH_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._display_name: str | None = None
        self._batch_days = max(1, batch_days)
        self._batch_ttl = max(0, batch_ttl_seconds)
        self._batch: dict[str, tuple[float, Any]] = {}
        self._batch_lock = Lock()

    # -- batching ----------------------------------------------------------
    def _window(self, kind: str) -> tuple[str, str]:
        days = min(self._batch_days, self.RANGE_LIMITS.get(kind, self._batch_days))
        today = date.today()
        return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()

    def _batched(self, kind: str, loader: Any) -> Any:
        now = time.time()
        cached = self._batch.get(kind)
        if cached is not None and cached[0] > now:
            return cached[1]
        with self._batch_lock:
            cached = self._batch.get(kind)
            if cached is not None and cached[0] > now:
                return cached[1]
            value = loader()
            self._batch[kind] = (now + self._batch_ttl, value)
            return value

    def _in_window(self, cdate: str, kind: str) -> bool:
        start, end = self._window(kind)
        return start <= cdate <= end

    def _by_day(self, rows: Any, key: str = "calendarDate") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    day = str(item.get(key) or "")[:10]
                    if day:
                        out[day] = item
        return out

    def _sleep_index(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            start, end = self._window("sleep")
            raw = self._client.get(
                "/sleep-service/sleep/dailySleepSummariesByDate",
                {"startDate": start, "endDate": end},
            )
            return self._by_day(raw)

        return self._batched("sleep", load)

    def _hrv_index(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            start, end = self._window("hrv")
            raw = self._client.get(f"/hrv-service/hrv/daily/{start}/{end}")
            rows = raw.get("hrvSummaries") if isinstance(raw, dict) else raw
            return self._by_day(rows)

        return self._batched("hrv", load)

    def _summary_index(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            start, end = self._window("summaries")
            raw = self._client.get(
                "/usersummary-service/v2/dailysummaries",
                {"startDate": start, "endDate": end},
            )
            rows = raw.get("data") if isinstance(raw, dict) else raw
            return self._by_day(rows)

        return self._batched("summaries", load)

    def _stress_index(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            start, end = self._window("stress")
            raw = self._client.get(f"/usersummary-service/stats/stress/daily/{start}/{end}")
            return {
                day: _flatten_values(item)
                for day, item in self._by_day(raw).items()
            }

        return self._batched("stress", load)

    # -- identity ----------------------------------------------------------
    @property
    def display_name(self) -> str | None:
        if self._display_name is None:
            profile = self.get_user_profile()
            name = (profile.get("userData") or {}).get("displayName") or profile.get(
                "displayName"
            )
            self._display_name = name if isinstance(name, str) else None
        return self._display_name

    # -- wellness ----------------------------------------------------------
    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        if self._in_window(cdate, "sleep"):
            item = self._sleep_index().get(cdate)
            if item is not None:
                return {"dailySleepDTO": item}
        raw = self._client.get("/sleep-service/sleep/dailySleep", {"date": cdate})
        if isinstance(raw, dict) and "dailySleepDTO" in raw:
            return raw
        return {"dailySleepDTO": raw} if isinstance(raw, dict) else {}

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        if self._in_window(cdate, "hrv"):
            item = self._hrv_index().get(cdate)
            if item is not None:
                return {"hrvSummary": item}
        raw = self._client.get(f"/hrv-service/hrv/{cdate}")
        if isinstance(raw, dict) and "hrvSummary" in raw:
            return raw
        return {"hrvSummary": raw} if isinstance(raw, dict) else {}

    def _daily_summary(self, cdate: str) -> dict[str, Any]:
        if self._in_window(cdate, "summaries"):
            item = self._summary_index().get(cdate)
            if item is not None:
                return item
        raw = self._client.get(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": cdate, "endDate": cdate},
        )
        return _first(raw)

    def get_rhr_day(self, cdate: str) -> dict[str, Any]:
        item = self._daily_summary(cdate)
        heart_rate = item.get("heartRate")
        value = heart_rate.get("restingValue") if isinstance(heart_rate, dict) else None
        if value is None:
            value = item.get("restingHeartRate")
        if value is None:
            metrics = ((item.get("allMetrics") or {}).get("metricsMap") or {}).get(
                "WELLNESS_RESTING_HEART_RATE"
            )
            if isinstance(metrics, list):
                for metric in metrics:
                    if isinstance(metric, dict) and isinstance(metric.get("value"), (int, float)):
                        value = metric["value"]
                        break
        if isinstance(value, (int, float)):
            return {"calendarDate": cdate, "restingHeartRate": value}
        return item

    def get_all_day_stress(self, cdate: str) -> dict[str, Any]:
        """Merge both official stress shapes into the flat dict the tools expect.

        ``/stats/stress/daily`` has real per-bucket durations but no maximum, and
        it nests them under ``values``. ``/dailysummaries`` has average + maximum
        plus proportions of the tracked day. Neither alone is complete.
        """
        stats: dict[str, Any]
        if self._in_window(cdate, "stress"):
            stats = dict(self._stress_index().get(cdate) or {})
        else:
            stats = _flatten_values(
                _first(self._client.get(f"/usersummary-service/stats/stress/daily/{cdate}/{cdate}"))
            )
        summary = self._daily_summary(cdate)
        stress = summary.get("stress") if isinstance(summary.get("stress"), dict) else {}
        chronology = summary.get("wellnessChronology")
        tracked_seconds = None
        if isinstance(chronology, dict) and isinstance(
            chronology.get("totalDurationInMillis"), (int, float)
        ):
            tracked_seconds = chronology["totalDurationInMillis"] / 1000

        merged = dict(stats)
        if merged.get("avgStressLevel") is None:
            merged["avgStressLevel"] = stress.get("avgLevel") or stats.get("overallStressLevel")
        if merged.get("maxStressLevel") is None:
            merged["maxStressLevel"] = stress.get("maxLevel")
        # The stats endpoint has no "activity" bucket; derive it from the tracked
        # day so the field is not silently zero.
        if merged.get("activityStressDuration") is None and tracked_seconds:
            share = stress.get("activityProportion")
            if isinstance(share, (int, float)):
                merged["activityStressDuration"] = round(share * tracked_seconds)
        return merged

    def get_body_battery(self, start: str, end: str) -> list[dict[str, Any]]:
        raw = self._client.get(
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": start, "endDate": end},
        )
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    # -- activities --------------------------------------------------------
    def get_activities(self, start: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        raw = self._client.get(
            "/activitylist-service/activities/search/activities",
            {"start": start, "limit": limit},
        )
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def get_activity(self, activity_id: int) -> dict[str, Any]:
        raw = self._client.get(f"/activity-service/activity/{activity_id}")
        return raw if isinstance(raw, dict) else {}

    def get_activity_splits(self, activity_id: int) -> dict[str, Any]:
        """Per-kilometre laps for one activity.

        ``/activity-service/activity/{id}/splits`` is 404 on this tier and the
        ``splitSummaries`` in the activity payload collapse to one whole-activity
        entry. ``/details`` does return the raw per-sample stream (distance, time,
        HR, cadence, elevation), so the laps are rebuilt from that and keep the
        same field names the normalizer already understands. Falls back to the
        collapsed summary — labelled, never silently passed off as laps.
        """
        try:
            details = self._client.get(f"/activity-service/activity/{activity_id}/details")
        except PersonalApiHttpError:
            details = None
        laps = laps_from_details(details) if isinstance(details, dict) else []
        if laps:
            return {"lapDTOs": laps, "lap_source": "details"}

        detail = self.get_activity(activity_id)
        splits = detail.get("splitSummaries")
        if isinstance(splits, list) and splits:
            return {
                "lapDTOs": [item for item in splits if isinstance(item, dict)],
                "lap_source": "activity_summary",
                "note": (
                    "Garmin's Personal API does not expose per-km laps for this activity; "
                    "this is the whole-activity summary as a single lap."
                ),
            }
        return {}

    def get_activity_workouts(self, activity_id: int) -> Any:
        return self._client.get(f"/activity-service/activity/{activity_id}/workouts")

    def download_activity_file(self, activity_id: int) -> bytes:
        """Original activity file (ZIP containing the .FIT) for one activity."""
        return self._client.get_bytes(f"/download-service/files/activity/{activity_id}")

    # -- profile & zones ---------------------------------------------------
    def get_user_profile(self) -> dict[str, Any]:
        settings = self._client.get("/userprofile-service/userprofile/settings")
        profile = settings if isinstance(settings, dict) else {}
        zones = self._heart_rate_zones()
        if zones:
            profile.setdefault("userData", {})
            if isinstance(profile["userData"], dict):
                profile["userData"].setdefault("heartRateZones", zones)
        return profile

    def _heart_rate_zones(self) -> list[dict[str, Any]]:
        raw = self._client.get("/biometric-service/heartRateZones/v2")
        if isinstance(raw, list):
            candidates: Any = raw
        elif isinstance(raw, dict):
            candidates = raw.get("heartRateZones") or raw.get("zones") or []
        else:
            candidates = []
        zones: list[dict[str, Any]] = []
        for item in candidates if isinstance(candidates, list) else []:
            if not isinstance(item, dict):
                continue
            zone = item.get("zone") or item.get("zoneNumber") or item.get("zoneIndex")
            low = item.get("low") or item.get("lowBpm") or item.get("zoneLowBoundary")
            high = item.get("high") or item.get("highBpm") or item.get("zoneHighBoundary")
            if zone is None and low is None and high is None:
                continue
            zones.append({"zone": zone, "low": low, "high": high})
        return zones

    # -- personal-only extras ---------------------------------------------
    def get_body_composition(self, start: str, end: str) -> Any:
        return self._client.get(
            f"/weight-service/weight/range/{start}/{end}", {"includeAll": "true"}
        )

    def get_daily_summaries(self, start: str, end: str) -> Any:
        return self._client.get(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": start, "endDate": end},
        )

    def get_spo2(self, cdate: str) -> Any:
        return self._client.get(f"/wellness-service/wellness/daily/spo2acclimation/{cdate}")

    def get_hydration(self, start: str, end: str) -> Any:
        return self._client.get(f"/usersummary-service/stats/hydration/daily/{start}/{end}")

    def get_steps(self, start: str, end: str) -> Any:
        return self._client.get(f"/usersummary-service/stats/steps/daily/{start}/{end}")

    def get_fitness_age(self, cdate: str | None = None) -> Any:
        day = cdate or date.today().isoformat()
        return self._client.get(f"/fitnessage-service/fitnessage/v2/{day}")

    # -- not available on the Personal API --------------------------------
    _UNSUPPORTED = {
        "get_training_status": "training status/load (Garmin-derived)",
        "get_training_readiness": "training readiness (Garmin-derived)",
        "get_race_predictions": "race predictions (Garmin-derived)",
        "get_max_metrics": "VO2 Max / max metrics (Garmin-derived)",
        "get_cycling_ftp": "cycling FTP (Garmin-derived)",
        "get_personal_record": "personal records (Garmin-derived)",
    }

    def __getattr__(self, name: str) -> Any:
        if name in self._UNSUPPORTED:
            what = self._UNSUPPORTED[name]

            def raise_unsupported(*_args: Any, **_kwargs: Any) -> Any:
                raise PersonalApiUnsupported(
                    f"{what} is not exposed by the Garmin Personal API; "
                    "use GARMIN_AUTH_MODE=unofficial for that metric."
                )

            return raise_unsupported
        raise AttributeError(name)
