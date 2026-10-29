from __future__ import annotations

import contextvars
import os
import re
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from .config import MAX_ACTIVITY_LIMIT, MAX_DAYS_ACTIVITIES, MAX_DAYS_HEALTH
from .garmin_client import date_range_iso, get_manager, today_iso
from .hybrid import capture_call
from .normalizers import (
    activity_type_key,
    format_duration,
    normalize_activity,
    round_number,
    sport_bucket,
)

ErrorList = list[dict[str, str]]
_FAILED = object()
_ZONE_KEY_RE = re.compile(r"(zone|threshold|lactate|ftp|vo2)", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    function: Callable[..., dict[str, Any]]
    parameters: dict[str, Any]


_KNOWN_PR_LABELS_BY_KEY = {
    "pr.label.1k.run": "Fastest 1K",
    "pr.label.1mile.run": "Fastest 1 mile",
    "pr.label.5k.run": "Fastest 5K",
    "pr.label.10k.run": "Fastest 10K",
    "pr.label.half.marathon": "Fastest half marathon",
    "pr.label.full.marathon": "Fastest marathon",
    "pr.label.longest.run": "Longest run",
    "pr.label.40k.cycling": "Fastest 40K ride",
    "pr.label.longest.ride": "Longest ride",
    "pr.label.max.elevation.gain": "Most elevation gained",
    "pr.label.best.20min.power": "Best 20-minute average power",
    "pr.label.most.steps.day": "Most steps in a day",
    "pr.label.most.steps.week": "Most steps in a week",
    "pr.label.most.steps.month": "Most steps in a month",
    "pr.label.longest.goal.streak": "Longest step goal streak",
}

_KNOWN_PR_LABELS_BY_TYPE_ID = {
    1: "Fastest 1K",
    2: "Fastest 1 mile",
    3: "Fastest 5K",
    4: "Fastest 10K",
    5: "Fastest half marathon",
    6: "Fastest marathon",
}


def _client() -> Any:
    return get_manager().client()


def _errors_payload(errors: ErrorList) -> dict[str, Any]:
    return {"errors": errors} if errors else {}


def _endpoint(
    key: str,
    label: str,
    factory: Callable[[], Any],
    default: Any,
    errors: ErrorList | None = None,
) -> Any:
    """Fetch one Garmin endpoint through the shared TTL cache.

    Failures are not cached: the sentinel result is evicted immediately so the
    next caller retries the endpoint.
    """
    manager = get_manager()

    def cached_factory() -> Any:
        # capture_call keeps hybrid attribution working: the factory may run in a
        # different thread (day loops) or not at all (cache hit -> replay below).
        value, sources = capture_call(
            manager.safe_call, factory, default=_FAILED, label=label, errors=errors
        )
        if value is not _FAILED:
            manager.remember_sources(key, sources)
        return value

    value = manager.cached(key, cached_factory)
    manager.replay_sources(key)
    if value is _FAILED:
        manager.cache.delete(key)
        return default
    return value


def _fetch_days(dates: list[str], fetch: Callable[[str], Any], max_workers: int = 4) -> list[Any]:
    if len(dates) <= 1:
        return [fetch(day) for day in dates]
    # ContextVars are not inherited by thread-pool workers, and copying inside the
    # worker would copy the worker's empty context. Copy here, in the parent, so
    # hybrid usage capture still sees the calls made by the day loops.
    contexts = [contextvars.copy_context() for _ in dates]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(dates))) as ex:
        futures = [
            ex.submit(context.run, fetch, day)
            for context, day in zip(contexts, dates, strict=True)
        ]
        return [future.result() for future in futures]


def _sleep_day(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"sleep_day:{day}",
        f"sleep:{day}",
        lambda: client.get_sleep_data(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _hrv_day(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"hrv_day:{day}",
        f"hrv:{day}",
        lambda: client.get_hrv_data(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _rhr_day(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"rhr_day:{day}",
        f"resting_heart_rate:{day}",
        lambda: client.get_rhr_day(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _stress_day(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"stress_day:{day}",
        f"stress:{day}",
        lambda: client.get_all_day_stress(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _training_readiness(day: str, errors: ErrorList | None = None) -> Any:
    client = _client()
    return _endpoint(
        f"training_readiness:{day}",
        f"training_readiness:{day}",
        lambda: client.get_training_readiness(day),
        None,
        errors,
    )


def _training_status(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"training_status:{day}",
        f"training_status:{day}",
        lambda: client.get_training_status(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _body_battery(start: str, end: str, errors: ErrorList | None = None) -> Any:
    client = _client()
    return _endpoint(
        f"body_battery:{start}:{end}",
        f"body_battery:{start}:{end}",
        lambda: client.get_body_battery(start, end),
        [],
        errors,
    )


def _body_battery_levels(item: dict[str, Any]) -> dict[str, Any]:
    """Highest/lowest/end-of-day level for one body battery report day.

    Garmin has no summary fields for these (verified 2026-10-29): the levels live only
    in bodyBatteryValuesArray as [timestamp_ms, level]. The end-of-day feedback event
    carries a label ("HIGH", "VERY_LOW"), not a number, so it is not used. The flat
    keys are kept as a fallback.
    """
    levels = [
        level
        for _, level in _pairs(item.get("bodyBatteryValuesArray"))
        if isinstance(level, (int, float))
    ]
    if not levels:
        return {
            "highest": item.get("highestBatteryLevel"),
            "lowest": item.get("lowestBatteryLevel"),
            "end_of_day": item.get("endOfDayBatteryLevel"),
        }
    return {"highest": max(levels), "lowest": min(levels), "end_of_day": levels[-1]}


def _max_metrics(day: str, errors: ErrorList | None = None) -> Any:
    client = _client()
    return _endpoint(
        f"max_metrics:{day}",
        f"max_metrics:{day}",
        lambda: client.get_max_metrics(day),
        None,
        errors,
    )


def _activities_raw(
    limit: int = 200, errors: ErrorList | None = None, start: int = 0
) -> list[dict[str, Any]]:
    client = _client()
    raw = _endpoint(
        f"activities_raw:{start}:{limit}",
        f"activities:{start}:{limit}",
        lambda: client.get_activities(start, limit),
        [],
        errors,
    )
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _activities_paged(
    max_items: int,
    errors: ErrorList | None = None,
    page_size: int = 200,
    stop_before: str | None = None,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Walk Garmin's activity pages — a single call is capped around 200 items.

    Without this, asking for more than one page silently returned only the newest
    200 activities, so a true "all time" range was impossible.

    `stop_before` (YYYY-MM-DD) stops paging once a page reaches past that date, which
    is what makes an explicit `start_date` window usable: the newest N activities may
    all be newer than the window being asked for. `max_pages` keeps a runaway loop
    from hammering the API.
    """
    if max_items <= 0:
        return []
    out: list[dict[str, Any]] = []
    start = 0
    for _ in range(max(1, max_pages)):
        page = _activities_raw(page_size, errors, start=start)
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        if stop_before:
            oldest = (
                min(
                    (item.get("startTimeLocal") or "")[:10]
                    for item in page
                    if item.get("startTimeLocal")
                )
                if any(item.get("startTimeLocal") for item in page)
                else ""
            )
            if oldest and oldest < stop_before:
                break
        start += page_size
        if len(out) >= max_items:
            break
    return out[:max_items]


def _extract_resting_heart_rate_bpm(raw: Any) -> int | None:
    if not isinstance(raw, dict):
        return None

    for key in (
        "restingHeartRate",
        "restingHR",
        "restingHeartRateBpm",
        "averageRestingHeartRate",
        "value",
    ):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return int(round(value))

    try:
        metrics = raw["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"]
    except (KeyError, TypeError):
        metrics = None

    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            value = metric.get("value")
            if isinstance(value, (int, float)):
                return int(round(value))
    return None


def _avg(values: list[int | float]) -> float | None:
    return round_number(sum(values) / len(values), 1) if values else None


def _personal_record_type_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _personal_record_label_key(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("key", "labelKey", "typeLabelKey", "displayName", "label"):
            label = value.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()
    return None


def _personal_record_value_unit(label: str | None) -> str | None:
    """Garmin mixes types inside `value`, depending on the record type.

    Durations arrive as "h:mm:ss" strings, distances and elevation as numbers in
    metres, power in watts, steps/streaks as plain counts. Without this, callers
    cannot tell whether 3000 means 3 km or 50 minutes (same class of bug as #3).
    """
    if not label:
        return None
    lowered = label.lower()
    if lowered.startswith("fastest"):
        return "duration"
    if "power" in lowered:
        return "watts"
    if lowered.startswith("longest run") or lowered.startswith("longest ride"):
        return "distance_m"
    if "elevation" in lowered:
        return "elevation_m"
    if "steps" in lowered:
        return "steps"
    if "streak" in lowered:
        return "days"
    return None


def _personal_record_label(label_key: str | None, type_id: int | None) -> str | None:
    if label_key:
        if label_key in _KNOWN_PR_LABELS_BY_KEY:
            return _KNOWN_PR_LABELS_BY_KEY[label_key]
        if not any(separator in label_key for separator in (".", "_", "-")):
            return label_key
        cleaned = re.sub(r"^pr[._-]label[._-]", "", label_key, flags=re.IGNORECASE)
        cleaned = re.sub(r"[._-]+", " ", cleaned).strip()
        return cleaned.title() if cleaned else label_key
    if type_id is not None:
        return _KNOWN_PR_LABELS_BY_TYPE_ID.get(type_id)
    return None


def _personal_record_type_map(client: Any, manager: Any) -> dict[int, dict[str, Any]]:
    display_name = getattr(client, "display_name", None)
    if not display_name or not hasattr(client, "connectapi"):
        return {}

    def fetch() -> Any:
        return manager.safe_call(
            lambda: client.connectapi(
                f"/personalrecord-service/personalrecordtype/prtypes/{display_name}"
            ),
            default=[],
            label="personal_record_types",
        )

    raw = manager.cached("personal_record_types", fetch)
    if not isinstance(raw, list):
        return {}

    type_map = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        type_id = _personal_record_type_id(item.get("id") or item.get("typeId"))
        if type_id is not None:
            type_map[type_id] = item
    return type_map


def get_recovery() -> dict[str, Any]:
    """Current recovery state: readiness, HRV, sleep, body battery, and status."""
    today = today_iso()
    dates = date_range_iso(7)
    errors: ErrorList = []

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_readiness = ex.submit(_training_readiness, today, errors)
        f_status = ex.submit(_training_status, today, errors)
        f_body = ex.submit(_body_battery, dates[-1], today, errors)
        f_rhr = ex.submit(_rhr_day, today, errors)
        hrv_days = _fetch_days(dates, lambda day: _hrv_day(day, errors))
        sleep_days = _fetch_days(dates, lambda day: _sleep_day(day, errors))
        hrv_raw = dict(zip(dates, hrv_days, strict=True))
        sleep_raw = dict(zip(dates, sleep_days, strict=True))
        readiness_raw = f_readiness.result()
        status_raw = f_status.result()
        body_raw = f_body.result()
        rhr_raw = f_rhr.result()

    readiness = None
    item = readiness_raw[0] if isinstance(readiness_raw, list) and readiness_raw else readiness_raw
    if isinstance(item, dict):
        readiness = {
            "score": item.get("score"),
            "level": item.get("level"),
            "feedback_short": item.get("feedbackShort"),
            "feedback_long": item.get("feedbackLong"),
            "recovery_time_hours": _minutes_to_hours(item.get("recoveryTime")),
            "sleep_score": item.get("sleepScore"),
            "hrv_weekly_avg": item.get("hrvWeeklyAverage"),
        }

    hrv_daily = []
    for day in dates:
        raw = hrv_raw.get(day)
        if not isinstance(raw, dict):
            continue
        summary = raw.get("hrvSummary") or {}
        if summary.get("lastNightAvg") is None and summary.get("weeklyAvg") is None:
            continue
        hrv_daily.append(
            {
                "date": day,
                "last_night_avg_ms": summary.get("lastNightAvg"),
                "weekly_avg_ms": summary.get("weeklyAvg"),
                "status": summary.get("status"),
            }
        )
    hrv_values = [
        entry["last_night_avg_ms"]
        for entry in hrv_daily
        if isinstance(entry.get("last_night_avg_ms"), (int, float))
    ]

    sleep_hours = []
    sleep_scores = []
    for day in dates:
        raw = sleep_raw.get(day)
        if not isinstance(raw, dict):
            continue
        dto = raw.get("dailySleepDTO") or {}
        seconds = dto.get("sleepTimeSeconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            sleep_hours.append(seconds / 3600)
        overall = (dto.get("sleepScores") or {}).get("overall")
        score = overall.get("value") if isinstance(overall, dict) else None
        if isinstance(score, (int, float)):
            sleep_scores.append(score)

    body_battery = None
    if isinstance(body_raw, list) and body_raw and isinstance(body_raw[-1], dict):
        latest = body_raw[-1]
        body_battery = {
            "charged": latest.get("charged"),
            "drained": latest.get("drained"),
            **_body_battery_levels(latest),
        }

    resting_heart_rate = _extract_resting_heart_rate_bpm(rhr_raw)

    training_status = None
    if isinstance(status_raw, dict):
        latest_map = (status_raw.get("mostRecentTrainingStatus") or {}).get(
            "latestTrainingStatusData"
        ) or {}
        if latest_map:
            first = next(iter(latest_map.values()))
            if isinstance(first, dict):
                training_status = {
                    "status": first.get("trainingStatus"),
                    "feedback": first.get("trainingStatusFeedbackPhrase"),
                    "fitness_trend": first.get("fitnessTrend"),
                }

    return {
        "date": today,
        "training_readiness": readiness,
        "hrv": {
            "weekly_avg_ms": round_number(sum(hrv_values) / len(hrv_values), 1)
            if hrv_values
            else None,
            "status_latest": hrv_daily[0]["status"] if hrv_daily else None,
            "daily": hrv_daily,
        },
        "sleep": {
            "weekly_avg_hours": round_number(sum(sleep_hours) / len(sleep_hours), 2)
            if sleep_hours
            else None,
            "weekly_avg_score": round_number(sum(sleep_scores) / len(sleep_scores), 1)
            if sleep_scores
            else None,
        },
        "body_battery_today": body_battery,
        "resting_heart_rate_bpm": resting_heart_rate,
        "training_status": training_status,
        **_errors_payload(errors),
    }


def get_sleep(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Sleep for the last N days: duration, score, stages, naps, overnight HRV/SpO2/
    respiration/stress, bedtime and wake time, and Garmin's sleep need."""
    dates = date_range_iso(days)
    errors: ErrorList = []
    raws = _fetch_days(dates, lambda d: _sleep_day(d, errors))
    daily = [_normalize_sleep_day(day, raw) for day, raw in zip(dates, raws, strict=True)]

    all_naps = [nap for d in daily for nap in d["naps"]]
    latest_dto = (raws[0].get("dailySleepDTO") or {}) if raws else {}
    return {
        "period_days": len(dates),
        "avg_hours": _mean([d["hours"] for d in daily], 2),
        "avg_score": _mean([d["score"] for d in daily]),
        "avg_deep_minutes": _mean([d["stages"]["deep"]["minutes"] for d in daily], 0),
        "avg_rem_minutes": _mean([d["stages"]["rem"]["minutes"] for d in daily], 0),
        "avg_overnight_hrv_ms": _mean([d["overnight_hrv_ms"] for d in daily]),
        "avg_spo2": _mean([d["spo2"]["avg"] for d in daily]),
        "avg_respiration": _mean([d["respiration"]["avg"] for d in daily]),
        "nap_count": len(all_naps),
        "total_nap_minutes": sum(d["nap_minutes"] or 0 for d in daily),
        "next_sleep_need": _sleep_need(latest_dto.get("nextSleepNeed")),
        "daily": daily,
        **_errors_payload(errors),
    }


def get_resting_heart_rate(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 30,
) -> dict[str, Any]:
    """Daily resting heart rate with 7-day and 30-day trend summaries, up to 30 days."""
    safe_days = max(1, min(days, MAX_DAYS_HEALTH))
    dates = date_range_iso(safe_days)
    errors: ErrorList = []
    daily = []
    for day, raw in zip(dates, _fetch_days(dates, lambda d: _rhr_day(d, errors)), strict=True):
        daily.append({"date": day, "bpm": _extract_resting_heart_rate_bpm(raw)})

    values = [item["bpm"] for item in daily if isinstance(item.get("bpm"), int)]
    week_values = [item["bpm"] for item in daily[:7] if isinstance(item.get("bpm"), int)]
    month_values = [item["bpm"] for item in daily[:30] if isinstance(item.get("bpm"), int)]
    latest = next((item for item in daily if isinstance(item.get("bpm"), int)), None)
    latest_bpm = latest["bpm"] if latest else None
    weekly_avg = _avg(week_values)
    monthly_avg = _avg(month_values)

    return {
        "period_days": safe_days,
        "latest": latest,
        "latest_bpm": latest_bpm,
        "weekly_avg_bpm": weekly_avg,
        "monthly_avg_bpm": monthly_avg,
        "min_bpm": min(values) if values else None,
        "max_bpm": max(values) if values else None,
        "delta_latest_vs_weekly_avg_bpm": round_number(latest_bpm - weekly_avg, 1)
        if isinstance(latest_bpm, int) and isinstance(weekly_avg, (int, float))
        else None,
        "delta_latest_vs_monthly_avg_bpm": round_number(latest_bpm - monthly_avg, 1)
        if isinstance(latest_bpm, int) and isinstance(monthly_avg, (int, float))
        else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def get_stress(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Daily all-day stress levels and stress duration buckets."""
    dates = date_range_iso(days)
    errors: ErrorList = []
    daily = []
    for day, raw in zip(dates, _fetch_days(dates, lambda d: _stress_day(d, errors)), strict=True):
        daily.append(
            {
                "date": day,
                "stress_avg": raw.get("avgStressLevel") or raw.get("overallStressLevel"),
                "max_stress": raw.get("maxStressLevel"),
                "rest_minutes": round_number((raw.get("restStressDuration") or 0) / 60, 1),
                "low_minutes": round_number((raw.get("lowStressDuration") or 0) / 60, 1),
                "medium_minutes": round_number((raw.get("mediumStressDuration") or 0) / 60, 1),
                "high_minutes": round_number((raw.get("highStressDuration") or 0) / 60, 1),
                "activity_minutes": round_number((raw.get("activityStressDuration") or 0) / 60, 1),
            }
        )
    valid = [d["stress_avg"] for d in daily if isinstance(d.get("stress_avg"), (int, float))]
    return {
        "period_days": len(dates),
        "period_avg_stress": round_number(sum(valid) / len(valid), 1) if valid else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def _fetch_recent_activities(
    days: int,
    errors: ErrorList | None = None,
    max_items: int = MAX_ACTIVITY_LIMIT,
    start_date: str = "",
    end_date: str = "",
) -> list[dict[str, Any]]:
    """Activities in a window. An explicit start_date/end_date wins over `days`."""
    if start_date or end_date:
        lower = start_date or "0000-01-01"
        upper = end_date or today_iso()
    else:
        lower = date_range_iso(days)[-1]
        upper = ""
    activities = []
    for item in _activities_paged(max_items, errors, stop_before=lower if start_date else None):
        started = item.get("startTimeLocal") or ""
        day = started[:10]
        if not day or day < lower:
            continue
        if upper and day > upper:
            continue
        normalized = normalize_activity(item)
        if normalized:
            activities.append(normalized)
    activities.sort(key=lambda item: item.get("date") or "", reverse=True)
    return activities


def _recent_activity_dates(days: int = 120, errors: ErrorList | None = None) -> list[str]:
    dates: list[str] = []
    cutoff = date_range_iso(days)[-1]
    for item in _activities_raw(200, errors):
        started = item.get("startTimeLocal") or ""
        day = started[:10]
        if not day or day < cutoff or day in dates:
            continue
        dates.append(day)
    return dates


def _supports_derived_metrics() -> bool:
    """Whether the active backend can answer Garmin-derived metrics at all."""
    return getattr(_client(), "supports_derived_metrics", True)


def _latest_max_metrics(
    errors: ErrorList | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    if not _supports_derived_metrics():
        return None, None
    candidate_dates = [today_iso(), *_recent_activity_dates(errors=errors)]
    seen: set[str] = set()
    for day in candidate_dates:
        if day in seen:
            continue
        seen.add(day)
        raw = _max_metrics(day, errors)
        item = raw[0] if isinstance(raw, list) and raw else raw
        if isinstance(item, dict) and item:
            return day, item
    return None, None


def get_recent_activities(
    days: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_DAYS_ACTIVITIES,
            description="How many days back to include. Ignored when start_date is given.",
        ),
    ] = 14,
    limit: Annotated[
        int,
        Field(ge=1, le=MAX_ACTIVITY_LIMIT, description="Maximum number of activities to return."),
    ] = 50,
    start_date: Annotated[
        str, Field(description="YYYY-MM-DD. Explicit window start (overrides days).")
    ] = "",
    end_date: Annotated[
        str, Field(description="YYYY-MM-DD. Window end, inclusive. Defaults to today.")
    ] = "",
) -> dict[str, Any]:
    """Recent Garmin activities with normalized sport-aware summary fields.

    Pass start_date/end_date for a specific historical window (both inclusive);
    otherwise the `days` lookback is used. Explicit dates make a past window
    queryable without pulling every newer activity first.
    """
    safe_limit = max(1, min(limit, MAX_ACTIVITY_LIMIT))
    safe_days = max(1, min(days, MAX_DAYS_ACTIVITIES))
    errors: ErrorList = []

    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if value and not _ISO_DATE_RE.match(value.strip()):
            return {
                "error": f"{name} must be YYYY-MM-DD",
                "count": 0,
                "activities": [],
                **_errors_payload(errors),
            }
    if start_date and end_date and start_date.strip() > end_date.strip():
        return {
            "error": "start_date is after end_date",
            "count": 0,
            "activities": [],
            **_errors_payload(errors),
        }

    explicit = bool(start_date or end_date)
    activities = _fetch_recent_activities(
        safe_days, errors, safe_limit, start_date.strip(), end_date.strip()
    )
    out: dict[str, Any] = {
        "period_days": safe_days,
        "count": min(len(activities), safe_limit),
        "activities": activities[:safe_limit],
    }
    if explicit:
        out["window"] = {
            "start": start_date.strip() or date_range_iso(safe_days)[-1],
            "end": end_date.strip() or today_iso(),
        }
    out.update(_errors_payload(errors))
    return out


def get_activity_detail(activity_id: int) -> dict[str, Any]:
    """Detailed summary for one activity by Garmin activity ID, with weather and time in
    heart-rate and power zones."""
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    raw = manager.cached(
        f"activity_detail:{activity_id}",
        lambda: manager.safe_call(
            lambda: client.get_activity(activity_id),
            default={},
            errors=errors,
            label=f"activity:{activity_id}",
        ),
    )
    if not isinstance(raw, dict) or not raw:
        return {
            "activity_id": activity_id,
            "error": "activity not found or inaccessible",
            **_errors_payload(errors),
        }
    summary = raw.get("summaryDTO") or raw.get("summary") or raw
    type_key = activity_type_key(raw)
    return {
        "activity_id": activity_id,
        "name": raw.get("activityName") or summary.get("activityName"),
        "sport": sport_bucket(type_key),
        "type_key": type_key,
        "summary": _without_gps(normalize_activity(summary) or summary),
        "training_effect": {
            "aerobic": summary.get("trainingEffect") or summary.get("aerobicTrainingEffect"),
            "anaerobic": summary.get("anaerobicTrainingEffect"),
            "label": summary.get("trainingEffectLabel"),
            "aerobic_message": summary.get("aerobicTrainingEffectMessage"),
            "anaerobic_message": summary.get("anaerobicTrainingEffectMessage"),
        },
        "weather": _activity_weather(activity_id, errors),
        "hr_time_in_zones": _activity_time_in_zones(
            activity_id, "hr", lambda: client.get_activity_hr_in_timezones(activity_id), errors
        ),
        "power_time_in_zones": _activity_time_in_zones(
            activity_id,
            "power",
            lambda: client.get_activity_power_in_timezones(activity_id),
            errors,
        ),
        "raw_keys": sorted(raw.keys())[:80],
        **_errors_payload(errors),
    }


def _minutes_to_hours(value: Any) -> float | None:
    """Garmin's recoveryTime is in MINUTES; the field we expose is in hours (issue #3)."""
    minutes = _number(value)
    return round(minutes / 60, 1) if minutes is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_lap(lap: dict[str, Any], index: int) -> dict[str, Any]:
    """Normalize one Garmin lap/split entry. Missing values stay null, never 0."""
    distance_m = _number(lap.get("distance"))
    duration_s = _number(lap.get("duration"))
    if duration_s is None:
        duration_s = _number(lap.get("movingDuration"))
    speed = _number(lap.get("averageSpeed"))
    if speed is None:
        speed = _number(lap.get("averageMovingSpeed"))
    pace_seconds = 1000.0 / speed if speed and speed > 0 else None
    if pace_seconds is None and distance_m and duration_s and distance_m > 0:
        pace_seconds = duration_s / (distance_m / 1000)
    hr_avg = _number(lap.get("averageHR"))
    hr_max = _number(lap.get("maxHR"))
    cadence = _number(
        lap.get("averageRunCadence")
        or lap.get("averageRunningCadenceInStepsPerMinute")
        or lap.get("averageBikingCadenceInRevPerMinute")
    )
    return {
        "index": index,
        "distance_km": round_number(distance_m / 1000, 2) if distance_m is not None else None,
        "duration": format_duration(duration_s),
        "duration_seconds": int(duration_s) if duration_s is not None else None,
        "pace": _format_pace(pace_seconds),
        "pace_seconds_per_km": round_number(pace_seconds, 1),
        "hr_avg": int(hr_avg) if hr_avg is not None else None,
        "hr_max": int(hr_max) if hr_max is not None else None,
        "cadence": round_number(cadence, 0),
        "elevation_gain_m": round_number(_number(lap.get("elevationGain")), 1),
    }


def _hr_drift(laps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Compare first-half to second-half average HR. Returns (drift, unavailable_reason)."""
    if len(laps) < 4:
        return None, f"needs at least 4 laps, got {len(laps)}"
    hr_values = [lap.get("hr_avg") for lap in laps]
    if any(not isinstance(value, (int, float)) for value in hr_values):
        return None, "not every lap reports an average heart rate"
    middle = len(laps) // 2
    first_avg = sum(hr_values[:middle]) / middle
    second_avg = sum(hr_values[middle:]) / (len(laps) - middle)
    if first_avg <= 0:
        return None, "first-half average heart rate is not usable"
    drift_pct = (second_avg - first_avg) / first_avg * 100
    if drift_pct <= 3:
        interpretation = "stabil"
    elif drift_pct <= 5:
        interpretation = "drift ringan"
    else:
        interpretation = "drift tinggi (cari penyebab: panas/dehidrasi/kelelahan)"
    return {
        "first_half_avg_hr": round_number(first_avg, 0),
        "second_half_avg_hr": round_number(second_avg, 0),
        "drift_pct": round(drift_pct, 1),
        "interpretation": interpretation,
    }, None


def get_activity_splits(activity_id: int) -> dict[str, Any]:
    """Per-lap splits for one activity: pace, HR, cadence, elevation and HR drift."""
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    raw = manager.cached(
        f"activity_splits:{activity_id}",
        lambda: manager.safe_call(
            lambda: client.get_activity_splits(activity_id),
            default={},
            errors=errors,
            label=f"splits:{activity_id}",
        ),
    )
    lap_dtos = raw.get("lapDTOs") if isinstance(raw, dict) else None
    if not isinstance(lap_dtos, list):
        return {
            "activity_id": activity_id,
            "error": "splits not found or inaccessible",
            **_errors_payload(errors),
        }
    laps = [
        _normalize_lap(lap, index)
        for index, lap in enumerate(lap_dtos, start=1)
        if isinstance(lap, dict)
    ]
    if not laps:
        return {
            "activity_id": activity_id,
            "error": "splits not found or inaccessible",
            **_errors_payload(errors),
        }

    distances = [lap["distance_km"] for lap in laps]
    durations = [lap["duration_seconds"] for lap in laps]
    total_distance_km = (
        round_number(sum(distances), 2) if all(value is not None for value in distances) else None
    )
    total_seconds = sum(durations) if all(value is not None for value in durations) else None
    hr_pairs = [
        (lap["hr_avg"], lap["duration_seconds"]) for lap in laps if lap["hr_avg"] is not None
    ]
    if not hr_pairs:
        avg_hr = None
    elif all(seconds for _, seconds in hr_pairs):
        weight = sum(seconds for _, seconds in hr_pairs)
        avg_hr = round_number(sum(hr * seconds for hr, seconds in hr_pairs) / weight, 0)
    else:
        avg_hr = round_number(sum(hr for hr, _ in hr_pairs) / len(hr_pairs), 0)
    cadences = [lap["cadence"] for lap in laps if lap["cadence"] is not None]
    elevations = [lap["elevation_gain_m"] for lap in laps if lap["elevation_gain_m"] is not None]
    hr_drift, hr_drift_note = _hr_drift(laps)
    summary = {
        "distance_km": total_distance_km,
        "duration": format_duration(total_seconds),
        "avg_pace": _format_pace(total_seconds / total_distance_km)
        if total_seconds and total_distance_km
        else None,
        "avg_hr": avg_hr,
        "avg_cadence": round_number(sum(cadences) / len(cadences), 0) if cadences else None,
        "elevation_gain_m": round_number(sum(elevations), 1) if elevations else None,
    }
    origin = {}
    if isinstance(raw, dict):
        for key in ("lap_source", "note"):
            if raw.get(key):
                origin[key] = raw[key]
    return {
        "activity_id": activity_id,
        "lap_count": len(laps),
        "laps": laps,
        "summary": summary,
        "hr_drift": hr_drift,
        **({"hr_drift_note": hr_drift_note} if hr_drift_note else {}),
        **origin,
        **_errors_payload(errors),
    }


def _run_activities(days: int, errors: ErrorList | None = None) -> list[dict[str, Any]]:
    return [
        activity
        for activity in _fetch_recent_activities(days, errors)
        if activity.get("sport") == "run"
    ]


def _pace_seconds_from_activity(activity: dict[str, Any]) -> float | None:
    distance = activity.get("distance_km")
    duration = activity.get("duration_min")
    if not isinstance(distance, (int, float)) or not isinstance(duration, (int, float)):
        return None
    if distance <= 0 or duration <= 0:
        return None
    return (duration * 60) / distance


def _format_pace(seconds_per_km: float | None) -> str | None:
    if seconds_per_km is None:
        return None
    minutes = int(seconds_per_km // 60)
    seconds = int(seconds_per_km % 60)
    return f"{minutes}:{seconds:02d}/km"


def _month_key(activity: dict[str, Any]) -> str:
    date_value = activity.get("date") or ""
    return str(date_value)[:7] if date_value else "unknown"


def get_running_summary(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_ACTIVITIES, description="Lookback window in days.")
    ] = 90,
) -> dict[str, Any]:
    """Running summary: sessions, distance, pace, HR, longest run, fastest run, monthly split."""
    safe_days = max(1, min(days, MAX_DAYS_ACTIVITIES))
    errors: ErrorList = []
    runs = _run_activities(safe_days, errors)
    total_distance = sum(activity.get("distance_km") or 0 for activity in runs)
    total_minutes = sum(activity.get("duration_min") or 0 for activity in runs)
    hr_values = [
        activity["hr_avg"] for activity in runs if isinstance(activity.get("hr_avg"), (int, float))
    ]
    longest = max(runs, key=lambda activity: activity.get("distance_km") or 0, default=None)
    paced_runs = [
        (activity, _pace_seconds_from_activity(activity))
        for activity in runs
        if _pace_seconds_from_activity(activity) is not None
    ]
    fastest = min(paced_runs, key=lambda item: item[1] or 999999, default=(None, None))[0]
    return {
        "period_days": safe_days,
        "sessions": len(runs),
        "total_distance_km": round_number(total_distance, 2),
        "total_duration_hours": round_number(total_minutes / 60, 2),
        "average_pace": _format_pace((total_minutes * 60) / total_distance)
        if total_distance
        else None,
        "average_hr": round_number(sum(hr_values) / len(hr_values), 0) if hr_values else None,
        "longest_run": _compact_activity(longest),
        "fastest_run": _compact_activity(fastest),
        "monthly": get_monthly_running_stats(months=max(1, min(12, (safe_days + 30) // 31)))[
            "months"
        ],
        **_errors_payload(errors),
    }


def get_monthly_running_stats(
    months: Annotated[
        int, Field(ge=1, le=12, description="How many recent months to summarize.")
    ] = 3,
) -> dict[str, Any]:
    """Monthly running stats for the last N calendar-ish months based on recent activities."""
    safe_months = max(1, min(months, 12))
    errors: ErrorList = []
    runs = _run_activities(safe_months * 31, errors)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        buckets[_month_key(run)].append(run)
    months_out = []
    for month in sorted(buckets.keys(), reverse=True)[:safe_months]:
        items = buckets[month]
        distance = sum(item.get("distance_km") or 0 for item in items)
        minutes = sum(item.get("duration_min") or 0 for item in items)
        hr_values = [
            item["hr_avg"] for item in items if isinstance(item.get("hr_avg"), (int, float))
        ]
        months_out.append(
            {
                "month": month,
                "sessions": len(items),
                "total_distance_km": round_number(distance, 2),
                "total_duration_hours": round_number(minutes / 60, 2),
                "average_pace": _format_pace((minutes * 60) / distance) if distance else None,
                "average_hr": round_number(sum(hr_values) / len(hr_values), 0)
                if hr_values
                else None,
            }
        )
    return {"months_requested": safe_months, "months": months_out, **_errors_payload(errors)}


def _compact_activity(activity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not activity:
        return None
    return {
        "activity_id": activity.get("activity_id"),
        "name": activity.get("name"),
        "date": activity.get("date"),
        "distance_km": activity.get("distance_km"),
        "duration": activity.get("duration"),
        "pace": _format_pace(_pace_seconds_from_activity(activity)),
        "hr_avg": activity.get("hr_avg"),
    }


def get_recent_load(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_ACTIVITIES, description="Lookback window in days.")
    ] = 28,
) -> dict[str, Any]:
    """Training volume aggregated by sport over the last N days."""
    safe_days = max(1, min(days, MAX_DAYS_ACTIVITIES))
    errors: ErrorList = []
    activities = _fetch_recent_activities(safe_days, errors, MAX_ACTIVITY_LIMIT)
    buckets: dict[str, dict[str, Any]] = {}
    for activity in activities:
        sport = activity.get("sport") or "other"
        bucket = buckets.setdefault(
            sport, {"sessions": 0, "total_km": 0.0, "total_minutes": 0.0, "hr_sum": 0, "hr_n": 0}
        )
        bucket["sessions"] += 1
        bucket["total_km"] += activity.get("distance_km") or 0
        bucket["total_minutes"] += activity.get("duration_min") or 0
        if isinstance(activity.get("hr_avg"), (int, float)):
            bucket["hr_sum"] += activity["hr_avg"]
            bucket["hr_n"] += 1
    by_sport = {}
    for sport, bucket in buckets.items():
        by_sport[sport] = {
            "sessions": bucket["sessions"],
            "total_km": round_number(bucket["total_km"], 2),
            "total_minutes": round_number(bucket["total_minutes"], 1),
            "total_hours": round_number(bucket["total_minutes"] / 60, 2),
            "avg_hr": round_number(bucket["hr_sum"] / bucket["hr_n"], 0)
            if bucket["hr_n"]
            else None,
        }
    return {"period_days": safe_days, "by_sport": by_sport, **_errors_payload(errors)}


def get_training_load() -> dict[str, Any]:
    """Garmin training load status, acute/chronic load, and load focus."""
    today = today_iso()
    errors: ErrorList = []
    raw = _training_status(today, errors)
    if not raw:
        return {
            "date": today,
            "source": "garmin_training_status",
            "available": False,
            "reason": "training status endpoint returned no data",
            **_errors_payload(errors),
        }

    acute = chronic = ratio = status = None
    load_balance = raw.get("mostRecentTrainingLoadBalance")
    if isinstance(load_balance, dict):
        acute = round_number(
            load_balance.get("acuteTrainingLoad")
            or load_balance.get("dailyTrainingLoadAcute")
            or load_balance.get("load")
            or load_balance.get("trainingLoad"),
            1,
        )
        chronic = round_number(
            load_balance.get("chronicTrainingLoad") or load_balance.get("dailyTrainingLoadChronic"),
            1,
        )
        ratio = round_number(
            load_balance.get("acuteChronicWorkloadRatio")
            or load_balance.get("dailyAcuteChronicWorkloadRatio"),
            2,
        )
        status = load_balance.get("acwrStatus") or load_balance.get("loadStatus")

    latest_map = (raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    if latest_map:
        first = next(iter(latest_map.values()))
        if isinstance(first, dict):
            atl = first.get("acuteTrainingLoadDTO") or {}
            acute = acute or round_number(atl.get("dailyTrainingLoadAcute"), 1)
            chronic = chronic or round_number(atl.get("dailyTrainingLoadChronic"), 1)
            ratio = ratio or round_number(atl.get("dailyAcuteChronicWorkloadRatio"), 2)
            status = status or atl.get("acwrStatus")

    return {
        "date": today,
        "source": "garmin_training_status",
        "available": any(value is not None for value in [acute, chronic, ratio, status]),
        "acute_load": acute,
        "chronic_load": chronic,
        "load_ratio": ratio,
        "acwr_status": status,
        "raw_sections_present": {
            "mostRecentTrainingLoadBalance": raw.get("mostRecentTrainingLoadBalance") is not None,
            "mostRecentTrainingStatus": raw.get("mostRecentTrainingStatus") is not None,
        },
        **_errors_payload(errors),
    }


def get_fitness() -> dict[str, Any]:
    """VO2 max, cycling FTP, and race predictions."""
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    race_raw = manager.cached(
        "race_predictions",
        lambda: manager.safe_call(
            lambda: client.get_race_predictions(),
            default=None,
            errors=errors,
            label="race_predictions",
        ),
    )
    ftp_raw = manager.cached(
        "cycling_ftp",
        lambda: manager.safe_call(
            lambda: client.get_cycling_ftp(),
            default=None,
            errors=errors,
            label="cycling_ftp",
        ),
    )

    vo2_run = vo2_bike = None
    metrics_date, item = _latest_max_metrics(errors)
    if isinstance(item, dict):
        generic = item.get("generic") or {}
        cycling = item.get("cycling") or {}
        vo2_run = round_number(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"), 1)
        vo2_bike = round_number(cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"), 1)

    race_item = race_raw[-1] if isinstance(race_raw, list) and race_raw else race_raw
    race = {}
    if isinstance(race_item, dict):
        for key_in, key_out in [
            ("time5K", "5k"),
            ("time10K", "10k"),
            ("timeHalfMarathon", "half_marathon"),
            ("timeMarathon", "marathon"),
        ]:
            seconds = race_item.get(key_in)
            if isinstance(seconds, (int, float)) and seconds > 0:
                race[key_out] = {"seconds": int(seconds)}

    return {
        "metrics_date": metrics_date,
        "vo2_max_running": vo2_run,
        "vo2_max_cycling": vo2_bike,
        "cycling_ftp_w": ftp_raw.get("functionalThresholdPower")
        if isinstance(ftp_raw, dict)
        else ftp_raw,
        "race_predictions": race or None,
        **_errors_payload(errors),
    }


def get_zones() -> dict[str, Any]:
    """Heart-rate zones (bpm ranges per sport, with max/resting/threshold HR used),
    running power zones, and thresholds from Garmin user settings."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint(
        "user_profile",
        "user_profile",
        lambda: client.get_user_profile(),
        {},
        errors,
    )
    user_data = raw.get("userData") if isinstance(raw, dict) else None
    zones: dict[str, Any] = {}
    if isinstance(user_data, dict):
        for key, value in user_data.items():
            if not isinstance(key, str) or value in (None, [], {}):
                continue
            if _ZONE_KEY_RE.search(key):
                zones[key] = value
    hr_raw = _endpoint("heart_rate_zones", "heart_rate_zones",
                       lambda: client.get_heart_rate_zones(), [], errors)
    power_raw = _endpoint("power_zones:RUNNING", "power_zones:running",
                          lambda: client.get_power_zones_for_sport("RUNNING"), {}, errors)
    hr_zones = _hr_zone_sets(hr_raw)
    power_zones = _power_zone_set(power_raw)
    available = bool(zones or hr_zones)
    return {
        "available": available,
        "source": "garmin_user_settings",
        "zones": zones or None,
        "heart_rate_zones": hr_zones,
        "running_power_zones": power_zones,
        "reason": None if available
        else "user settings did not expose zone data for this account",
        **_errors_payload(errors),
    }


def get_personal_records() -> dict[str, Any]:
    """Personal records returned by Garmin Connect."""
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    getter = getattr(client, "get_personal_record", None) or getattr(
        client, "get_personal_records", None
    )
    if getter is None:
        return {
            "count": 0,
            "records": [],
            "available": False,
            "reason": "garminconnect client does not expose a personal records endpoint",
        }
    raw = manager.cached(
        "personal_records",
        lambda: manager.safe_call(getter, default=[], errors=errors, label="personal_records"),
    )
    if isinstance(raw, dict):
        raw = (
            raw.get("personalRecords")
            or raw.get("userPersonalRecords")
            or raw.get("records")
            or raw.get("prRecords")
            or []
        )
    type_map = _personal_record_type_map(client, manager)
    records = []
    if isinstance(raw, list):
        for record in raw:
            if not isinstance(record, dict):
                continue
            type_id = _personal_record_type_id(record.get("typeId"))
            type_info = type_map.get(type_id) if type_id is not None else None
            label_key = (
                _personal_record_label_key(record.get("prTypeLabelKey"))
                or _personal_record_label_key(record.get("typeLabelKey"))
                or _personal_record_label_key(type_info)
            )
            label = _personal_record_label(label_key, type_id)
            records.append(
                {
                    "type_id": type_id,
                    "name": label,
                    "label": label,
                    "label_key": label_key,
                    "sport": type_info.get("sport") if isinstance(type_info, dict) else None,
                    "value": record.get("value"),
                    "value_unit": _personal_record_value_unit(label),
                    "date": record.get("prStartTimeGmtFormatted") or record.get("prStartTimeGmt"),
                }
            )
    return {"count": len(records), "records": records, "available": True, **_errors_payload(errors)}


def get_hrv_history(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 30,
) -> dict[str, Any]:
    """Daily HRV (last-night and weekly average) plus a baseline.

    get_recovery carries only a 7-day HRV window, which is fine for "how am I today"
    but useless for trend questions. This is the same data over a longer range.
    `daily` is newest-first, like every other daily array here.
    """
    safe_days = max(1, min(days, MAX_DAYS_HEALTH))
    dates = date_range_iso(safe_days)
    errors: ErrorList = []
    daily = []
    for day, raw in zip(dates, _fetch_days(dates, lambda d: _hrv_day(d, errors)), strict=True):
        summary = raw.get("hrvSummary") if isinstance(raw, dict) else None
        if not isinstance(summary, dict):
            continue
        if summary.get("lastNightAvg") is None and summary.get("weeklyAvg") is None:
            continue
        daily.append(
            {
                "date": day,
                "last_night_avg_ms": summary.get("lastNightAvg"),
                "weekly_avg_ms": summary.get("weeklyAvg"),
                "status": summary.get("status"),
            }
        )

    values = [
        entry["last_night_avg_ms"]
        for entry in daily
        if isinstance(entry.get("last_night_avg_ms"), (int, float))
    ]
    baseline = round_number(sum(values) / len(values), 1) if values else None
    # daily is newest-first, so the latest night is the first value.
    latest = values[0] if values else None
    return {
        "period_days": safe_days,
        "days_with_data": len(daily),
        "baseline_avg_ms": baseline,
        "latest_ms": latest,
        "delta_vs_baseline_ms": round_number(latest - baseline, 1)
        if latest is not None and baseline is not None
        else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def get_body_battery_range(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Daily body battery (charged, drained, highest, lowest, end of day).

    get_recovery only exposes today's levels; this walks the range endpoint so the
    history is available for trends. The raw per-timestamp arrays are summarised away
    because they are large and rarely useful. `daily` is newest-first, like every other
    daily array here.
    """
    safe_days = max(1, min(days, MAX_DAYS_HEALTH))
    dates = date_range_iso(safe_days)
    errors: ErrorList = []
    # date_range_iso() is newest-first, so the API range is (oldest, newest).
    raw = _body_battery(dates[-1], dates[0], errors)

    daily = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            day = str(item.get("date") or "")[:10]
            if not day:
                continue
            daily.append(
                {
                    "date": day,
                    "charged": item.get("charged"),
                    "drained": item.get("drained"),
                    **_body_battery_levels(item),
                }
            )
    # Match the rest of the tools: daily arrays are newest-first.
    daily.sort(key=lambda entry: entry["date"], reverse=True)

    return {
        "period_days": safe_days,
        "days_with_data": len(daily),
        "daily": daily,
        **_errors_payload(errors),
    }


def get_health_summary(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Compact health snapshot combining recovery, sleep, and stress."""
    return {
        "date": today_iso(),
        "recovery": get_recovery(),
        "sleep": get_sleep(days),
        "resting_heart_rate": get_resting_heart_rate(min(max(days, 1), MAX_DAYS_HEALTH)),
        "stress": get_stress(days),
    }


def get_full_snapshot(
    activity_days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_ACTIVITIES, description="Activity window in days.")
    ] = 14,
    load_days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_ACTIVITIES, description="Training load window in days.")
    ] = 28,
) -> dict[str, Any]:
    """Comprehensive Garmin data snapshot for holistic analysis.

    Every section carries a ``data_source`` field naming the backend that
    answered it (`personal`, `unofficial`, or `personal+unofficial` when one
    section needed both — with the per-endpoint map in ``source_detail``). In
    hybrid mode this makes it visible which numbers are official-API data and
    which came from the derived-metrics backend, instead of hiding a fallback.
    (``data_source`` rather than ``source``: some sections already use ``source``
    for their own provenance string, e.g. ``garmin_training_status``.)
    """
    manager = get_manager()
    sections: dict[str, Callable[[], dict[str, Any]]] = {
        "recovery": get_recovery,
        "sleep": lambda: get_sleep(7),
        "resting_heart_rate": lambda: get_resting_heart_rate(30),
        "stress": lambda: get_stress(7),
        "recent_activities": lambda: get_recent_activities(activity_days),
        "recent_load": lambda: get_recent_load(load_days),
        "training_load": get_training_load,
        "fitness": get_fitness,
        "running_summary": lambda: get_running_summary(activity_days),
        "zones": get_zones,
        "personal_records": get_personal_records,
    }
    out: dict[str, Any] = {
        "snapshot_date": today_iso(),
        "auth_backend": manager.backend,
        "activity_window_days": activity_days,
        "load_window_days": load_days,
    }
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            name: ex.submit(_section_with_source, fn, manager.backend)
            for name, fn in sections.items()
        }
        for name, future in futures.items():
            out[name] = future.result()

    out["section_sources"] = {
        name: section.get("data_source")
        for name, section in out.items()
        if isinstance(section, dict) and section.get("data_source")
    }
    mixed = sorted(
        name for name, source in out["section_sources"].items() if source == "personal+unofficial"
    )
    out["hybrid_fallbacks"] = mixed
    return out


def _section_with_source(
    fn: Callable[[], dict[str, Any]], backend: str
) -> dict[str, Any]:
    """Run one snapshot section and tag it with the backend(s) that answered."""
    from .hybrid import capture_call

    try:
        payload, sources = capture_call(fn)
    except Exception as exc:  # noqa: BLE001 - one bad section must not kill the snapshot
        return {"error": f"{type(exc).__name__}: {exc}", "data_source": backend}
    if not isinstance(payload, dict):
        return payload
    used = sorted(set(sources.values()))
    if not used:
        payload.setdefault("data_source", backend)
    elif len(used) == 1:
        payload.setdefault("data_source", used[0])
    else:
        payload.setdefault("data_source", "personal+unofficial")
        payload.setdefault("source_detail", dict(sorted(sources.items())))
    return payload


def _safe_section(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Personal API only tools
#
# These use endpoints that exist in the official Garmin Personal API but have no
# equivalent in the unofficial email/password flow, so they are unavailable on
# GARMIN_AUTH_MODE=unofficial. See docs/personal-api.md.
# ---------------------------------------------------------------------------

_PERSONAL_ONLY_HINT = (
    "This tool needs the official Garmin Personal API. Set GARMIN_PERSONAL_API_KEY "
    "(create one at https://connect.garmin.com/app/auth/settings/personalApiToken) and "
    "GARMIN_AUTH_MODE=personal, then restart the server. See docs/personal-api.md."
)


def _personal_only_error() -> dict[str, Any] | None:
    manager = get_manager()
    if manager.backend in {"personal", "hybrid", "demo"}:
        return None
    return {
        "error": "personal api required",
        "backend": manager.backend,
        "hint": _PERSONAL_ONLY_HINT,
    }


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the first list-of-dicts out of a payload, trying known envelope keys."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def get_body_composition(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 30,
) -> dict[str, Any]:
    """Weight (and body fat/muscle when the scale reports it) from Garmin Connect.

    Personal API only. Weight is what most fitness decisions here hinge on, and
    the unofficial flow never exposed it as a tool.
    """
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    dates = date_range_iso(max(1, min(days, MAX_DAYS_HEALTH)))
    errors: ErrorList = []
    raw = _endpoint(
        f"body_composition:{dates[-1]}:{dates[0]}",
        "body_composition",
        lambda: client.get_body_composition(dates[-1], dates[0]),
        {},
        errors,
    )
    rows = _rows(raw, "dateWeightList", "dailyWeightSummaries", "allWeightMetrics", "weights")
    daily = []
    for item in rows:
        day = str(
            item.get("calendarDate")
            or item.get("date")
            or item.get("summaryDate")
            or item.get("timestampGMT")
            or ""
        )[:10]
        if not day:
            continue
        weight_g = item.get("weight")
        weight_kg = (
            round_number(weight_g / 1000, 2)
            if isinstance(weight_g, (int, float)) and weight_g > 0
            else round_number(item.get("weightKg"), 2)
        )
        daily.append(
            {
                "date": day,
                "weight_kg": weight_kg,
                "bmi": round_number(item.get("bmi"), 1),
                "body_fat_pct": round_number(item.get("bodyFat"), 1),
                "body_muscle_kg": round_number(
                    item.get("muscleMass") / 1000
                    if isinstance(item.get("muscleMass"), (int, float))
                    else None,
                    2,
                ),
                "body_water_pct": round_number(item.get("bodyWater"), 1),
            }
        )
    daily.sort(key=lambda entry: entry["date"], reverse=True)
    weights = [
        entry["weight_kg"] for entry in daily if isinstance(entry["weight_kg"], (int, float))
    ]
    return {
        "period_days": len(dates),
        "days_with_data": len(daily),
        "avg_weight_kg": round_number(sum(weights) / len(weights), 2) if weights else None,
        "latest_weight_kg": daily[0]["weight_kg"] if daily else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def get_spo2(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Blood oxygen (SpO2) acclimation per day. Personal API only."""
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    errors: ErrorList = []
    daily = []
    for day in date_range_iso(max(1, min(days, MAX_DAYS_HEALTH))):
        raw = _endpoint(
            f"spo2:{day}",
            f"spo2:{day}",
            lambda day=day: client.get_spo2(day),
            {},
            errors,
        )
        if not isinstance(raw, dict) or not raw:
            daily.append({"date": day, "avg_spo2": None, "lowest_spo2": None})
            continue
        daily.append(
            {
                "date": day,
                "avg_spo2": round_number(raw.get("averageSpO2") or raw.get("avgSpO2"), 1),
                "lowest_spo2": round_number(raw.get("lowestSpO2"), 1),
                "latest_spo2": round_number(raw.get("latestSpO2"), 1),
            }
        )
    values = [entry["avg_spo2"] for entry in daily if isinstance(entry["avg_spo2"], (int, float))]
    return {
        "period_days": len(daily),
        "avg_spo2": round_number(sum(values) / len(values), 1) if values else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def get_hydration(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Logged daily fluid intake in millilitres. Personal API only."""
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    dates = date_range_iso(max(1, min(days, MAX_DAYS_HEALTH)))
    errors: ErrorList = []
    raw = _endpoint(
        f"hydration:{dates[-1]}:{dates[0]}",
        "hydration",
        lambda: client.get_hydration(dates[-1], dates[0]),
        {},
        errors,
    )
    daily = []
    for item in _rows(raw, "hydrationValues", "dailyHydration", "hydration"):
        day = str(item.get("calendarDate") or item.get("date") or "")[:10]
        if not day:
            continue
        value = item.get("valueInML") or item.get("value") or item.get("amountInML")
        goal = item.get("goalInML") or item.get("goal")
        daily.append(
            {
                "date": day,
                "intake_ml": round_number(value, 0) if isinstance(value, (int, float)) else None,
                "goal_ml": round_number(goal, 0) if isinstance(goal, (int, float)) else None,
            }
        )
    daily.sort(key=lambda entry: entry["date"], reverse=True)
    return {
        "period_days": len(dates),
        "days_with_data": len(daily),
        "daily": daily,
        **_errors_payload(errors),
    }


def get_fitness_age() -> dict[str, Any]:
    """Garmin's fitness age for today. Personal API only."""
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    raw = manager.cached(
        f"fitness_age:{today_iso()}",
        lambda: manager.safe_call(
            client.get_fitness_age,
            default={},
            errors=errors,
            label="fitness_age",
        ),
    )
    if not isinstance(raw, dict) or not raw:
        return {"error": "fitness age not available", **_errors_payload(errors)}
    return {
        "date": today_iso(),
        "fitness_age": raw.get("fitnessAge") or raw.get("chronologicalAge"),
        "chronological_age": raw.get("chronologicalAge"),
        "achievable_fitness_age": raw.get("achievableFitnessAge"),
        "previous_fitness_age": raw.get("previousFitnessAge"),
        **_errors_payload(errors),
    }


def get_steps_history(
    days: Annotated[
        int, Field(ge=1, le=MAX_DAYS_HEALTH, description="Lookback window in days.")
    ] = 7,
) -> dict[str, Any]:
    """Daily step totals and goals. Personal API only."""
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    dates = date_range_iso(max(1, min(days, MAX_DAYS_HEALTH)))
    errors: ErrorList = []
    raw = _endpoint(
        f"steps:{dates[-1]}:{dates[0]}",
        "steps",
        lambda: client.get_steps(dates[-1], dates[0]),
        {},
        errors,
    )
    daily = []
    for item in _rows(raw, "steps", "dailySteps", "stepsValues"):
        day = str(item.get("calendarDate") or item.get("date") or "")[:10]
        if not day:
            continue
        total = item.get("totalSteps") or item.get("steps") or item.get("value")
        daily.append(
            {
                "date": day,
                "steps": int(total) if isinstance(total, (int, float)) else None,
                "goal": int(item["stepGoal"])
                if isinstance(item.get("stepGoal"), (int, float))
                else None,
            }
        )
    daily.sort(key=lambda entry: entry["date"], reverse=True)
    totals = [entry["steps"] for entry in daily if isinstance(entry["steps"], (int, float))]
    return {
        "period_days": len(dates),
        "days_with_data": len(daily),
        "avg_steps": round_number(sum(totals) / len(totals), 0) if totals else None,
        "daily": daily,
        **_errors_payload(errors),
    }


def download_activity_file(
    activity_id: int,
    save_path: Annotated[
        str, Field(description="Where to write the .zip. Empty = activity-<id>.zip in cwd.")
    ] = "",
) -> dict[str, Any]:
    """Download the original activity file (ZIP containing the .FIT) for one activity.

    Personal API only. This is the raw recording — GPS track, HR, cadence, laps,
    sample by sample — which the unofficial flow cannot fetch. Garmin strips some
    fields it computes (VO2 Max, recovery time, performance condition) from files
    delivered through its API, so this is not identical to a manual download.
    """
    blocked = _personal_only_error()
    if blocked:
        return blocked
    client = _client()
    manager = get_manager()
    errors: ErrorList = []
    target = os.path.expanduser(save_path) if save_path else f"activity-{activity_id}.zip"
    payload = manager.safe_call(
        lambda: client.download_activity_file(activity_id),
        default=None,
        errors=errors,
        label=f"download:{activity_id}",
    )
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return {
            "activity_id": activity_id,
            "error": "activity file not available",
            **_errors_payload(errors),
        }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        return {
            "activity_id": activity_id,
            "error": f"could not write {target}: {exc}",
            "bytes": len(payload),
            **_errors_payload(errors),
        }
    return {
        "activity_id": activity_id,
        "saved_to": os.path.abspath(target),
        "bytes": len(payload),
        "format": "zip (contains the original .FIT)",
        **_errors_payload(errors),
    }


def _minutes(seconds: Any) -> int | None:
    if not isinstance(seconds, (int, float)):
        return None
    return int(round(seconds / 60))


def _local_ms_to_iso(value: Any) -> str | None:
    """Garmin's *TimestampLocal sleep fields are epoch ms already shifted to local time."""
    if not isinstance(value, (int, float)):
        return None
    return (
        datetime.fromtimestamp(value / 1000, tz=UTC)
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )


def _gmt_to_local_iso(value: Any, offset_minutes: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace(" ", "T").split(".")[0])
    except ValueError:
        return None
    if isinstance(offset_minutes, (int, float)):
        moment += timedelta(minutes=offset_minutes)
    return moment.isoformat(timespec="minutes")


def _clock_from_minutes(value: Any) -> str | None:
    """Format minutes-after-midnight (may be negative or exceed 24h) as HH:MM."""
    if not isinstance(value, (int, float)):
        return None
    total = int(value) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def _sleep_need(need: Any) -> dict[str, Any] | None:
    if not isinstance(need, dict) or not need:
        return None
    start = _clock_from_minutes(need.get("recommendedBedtimeStartMins"))
    end = _clock_from_minutes(need.get("recommendedBedtimeEndMins"))
    return {
        "date": need.get("calendarDate"),
        "hours": round_number(need["actual"] / 60, 2)
        if isinstance(need.get("actual"), (int, float))
        else None,
        "baseline_hours": round_number(need["baseline"] / 60, 2)
        if isinstance(need.get("baseline"), (int, float))
        else None,
        "recommended_bedtime": f"{start}-{end}" if start and end else start or end,
        "feedback": need.get("feedback"),
        "adjustments": {
            "training": need.get("trainingFeedback"),
            "sleep_history": need.get("sleepHistoryAdjustment"),
            "hrv": need.get("hrvAdjustment"),
            "nap": need.get("napAdjustment"),
        },
    }


def _normalize_nap(nap: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": _gmt_to_local_iso(nap.get("napStartTimestampGMT"), nap.get("napStartTimeOffset")),
        "end": _gmt_to_local_iso(nap.get("napEndTimestampGMT"), nap.get("napEndTimeOffset")),
        "minutes": _minutes(nap.get("napTimeSec")),
        "feedback": nap.get("napFeedback"),
    }


def _normalize_sleep_day(day: str, raw: dict[str, Any]) -> dict[str, Any]:
    dto = raw.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores")
    scores = scores if isinstance(scores, dict) else {}
    overall = scores.get("overall") if isinstance(scores.get("overall"), dict) else {}
    seconds = dto.get("sleepTimeSeconds")

    stages: dict[str, Any] = {}
    for name, seconds_key, score_key in (
        ("deep", "deepSleepSeconds", "deepPercentage"),
        ("light", "lightSleepSeconds", "lightPercentage"),
        ("rem", "remSleepSeconds", "remPercentage"),
        ("awake", "awakeSleepSeconds", None),
    ):
        stage: dict[str, Any] = {"minutes": _minutes(dto.get(seconds_key))}
        pct = scores.get(score_key) if score_key else None
        if isinstance(pct, dict):
            stage["percent"] = pct.get("value")
            stage["rating"] = pct.get("qualifierKey")
        stages[name] = stage

    score_factors = {
        name: factor.get("qualifierKey")
        for name, factor in (
            ("duration", scores.get("totalDuration")),
            ("stress", scores.get("stress")),
            ("awake_count", scores.get("awakeCount")),
            ("restlessness", scores.get("restlessness")),
        )
        if isinstance(factor, dict)
    }

    naps_raw = dto.get("dailyNapDTOS") or raw.get("dailyNapDTOS") or []
    naps = [_normalize_nap(nap) for nap in naps_raw if isinstance(nap, dict)]
    nap_seconds = dto.get("napTimeSeconds")
    if not isinstance(nap_seconds, (int, float)):
        nap_seconds = sum(
            nap["napTimeSec"]
            for nap in naps_raw
            if isinstance(nap, dict) and isinstance(nap.get("napTimeSec"), (int, float))
        )

    alignment = dto.get("sleepAlignment")
    return {
        "date": day,
        "hours": round_number(seconds / 3600, 2) if isinstance(seconds, (int, float)) else None,
        "score": overall.get("value"),
        "score_rating": overall.get("qualifierKey"),
        "score_factors": score_factors or None,
        "feedback": dto.get("sleepScoreFeedback"),
        "insight": dto.get("sleepScorePersonalizedInsight"),
        "bedtime": _local_ms_to_iso(dto.get("sleepStartTimestampLocal")),
        "wake_time": _local_ms_to_iso(dto.get("sleepEndTimestampLocal")),
        "stages": stages,
        "awake_count": dto.get("awakeCount"),
        "restless_moments": raw.get("restlessMomentsCount"),
        "avg_sleep_hr": dto.get("avgHeartRate"),
        "resting_hr": raw.get("restingHeartRate"),
        "overnight_hrv_ms": raw.get("avgOvernightHrv"),
        "hrv_status": raw.get("hrvStatus"),
        "spo2": {
            "avg": dto.get("averageSpO2Value"),
            "lowest": dto.get("lowestSpO2Value"),
            "highest": dto.get("highestSpO2Value"),
        },
        "respiration": {
            "avg": dto.get("averageRespirationValue"),
            "lowest": dto.get("lowestRespirationValue"),
            "highest": dto.get("highestRespirationValue"),
        },
        "breathing_disruption": dto.get("breathingDisruptionSeverity"),
        "avg_sleep_stress": dto.get("avgSleepStress"),
        "body_battery_change": raw.get("bodyBatteryChange"),
        "skin_temp_deviation_c": raw.get("avgSkinTempDeviationC"),
        "schedule_alignment": alignment.get("status") if isinstance(alignment, dict) else None,
        "sleep_need": _sleep_need(dto.get("sleepNeed")),
        "nap_minutes": _minutes(nap_seconds) if raw else None,
        "naps": naps,
    }


def _mean(values: list[Any], ndigits: int = 1) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float))]
    return round_number(sum(numbers) / len(numbers), ndigits) if numbers else None


_GPS_KEY_RE = re.compile(r"(latitude|longitude|polyline)", re.IGNORECASE)


def _without_gps(data: Any) -> Any:
    """Drop GPS fields so routes and home locations never reach the LLM."""
    if isinstance(data, dict):
        return {k: _without_gps(v) for k, v in data.items() if not _GPS_KEY_RE.search(str(k))}
    if isinstance(data, list):
        return [_without_gps(v) for v in data]
    return data


def _activity_weather(activity_id: int, errors: ErrorList) -> dict[str, Any] | None:
    client = _client()
    raw = _endpoint(
        f"activity_weather:{activity_id}",
        f"activity_weather:{activity_id}",
        lambda: client.get_activity_weather(activity_id),
        {},
        errors,
    )
    if not isinstance(raw, dict) or not raw:
        return None
    wind = raw.get("windSpeed")
    station = raw.get("weatherStationDTO") or {}
    condition = raw.get("weatherTypeDTO") or {}
    # Garmin reports weather in imperial units: Fahrenheit and mph.
    return {
        "temp_c": _fahrenheit_to_c(raw.get("temp")),
        "feels_like_c": _fahrenheit_to_c(raw.get("apparentTemp")),
        "dew_point_c": _fahrenheit_to_c(raw.get("dewPoint")),
        "humidity_percent": raw.get("relativeHumidity"),
        "wind_kmh": round_number(wind * 1.609344, 0) if isinstance(wind, (int, float)) else None,
        "wind_direction": raw.get("windDirectionCompassPoint"),
        "condition": condition.get("desc"),
        "station": station.get("name"),
    }


def _activity_time_in_zones(
    activity_id: int, kind: str, factory: Callable[[], Any], errors: ErrorList
) -> list[dict[str, Any]]:
    raw = _endpoint(
        f"activity_{kind}_zones:{activity_id}",
        f"activity_{kind}_zones:{activity_id}",
        factory,
        [],
        errors,
    )
    zones = [z for z in raw if isinstance(z, dict)] if isinstance(raw, list) else []
    return [
        {
            "zone": z.get("zoneNumber"),
            "low_boundary": z.get("zoneLowBoundary"),
            "minutes": round_number(z["secsInZone"] / 60, 1)
            if isinstance(z.get("secsInZone"), (int, float))
            else None,
        }
        for z in sorted(zones, key=lambda z: z.get("zoneNumber") or 0)
    ]


def _stream_rows(raw: Any) -> list[dict[str, Any]]:
    """Turn Garmin's activity detail stream into a list of {metric_key: value} samples."""
    if not isinstance(raw, dict):
        return []
    descriptors = raw.get("metricDescriptors") or []
    index = {
        d["key"]: d["metricsIndex"]
        for d in descriptors
        if isinstance(d, dict) and "key" in d and "metricsIndex" in d
    }
    rows = []
    for sample in raw.get("activityDetailMetrics") or []:
        values = sample.get("metrics") if isinstance(sample, dict) else None
        if not isinstance(values, list):
            continue
        rows.append({k: values[i] if i < len(values) else None for k, i in index.items()})
    return [r for r in rows if _number(r.get("sumDistance")) is not None
            and _number(r.get("sumDuration")) is not None]


def _positive_avg(samples: list[dict[str, Any]], key: str, ndigits: int = 0) -> float | None:
    values = [v for v in (_number(s.get(key)) for s in samples) if v is not None and v > 0]
    return round_number(sum(values) / len(values), ndigits) if values else None


def _elevation_change(
    samples: list[dict[str, Any]], threshold_m: float = 1.0
) -> tuple[float, float]:
    """Elevation gain/loss, ignoring altimeter noise smaller than ``threshold_m``."""
    gain = loss = 0.0
    reference = None
    for s in samples:
        elevation = _number(s.get("directElevation"))
        if elevation is None:
            continue
        if reference is None:
            reference = elevation
            continue
        delta = elevation - reference
        if abs(delta) >= threshold_m:
            gain += max(delta, 0)
            loss += max(-delta, 0)
            reference = elevation
    return gain, loss


def _summarize_split(
    index: int, samples: list[dict[str, Any]], start_distance: float, start_time: float
) -> dict[str, Any]:
    last = samples[-1]
    distance = _number(last.get("sumDistance")) - start_distance
    seconds = _number(last.get("sumDuration")) - start_time
    hr_values = [v for v in (_number(s.get("directHeartRate")) for s in samples) if v]
    gap = _positive_avg(samples, "directGradeAdjustedSpeed", 3)
    gain, loss = _elevation_change(samples)
    return {
        "split": index,
        "distance_m": round_number(distance, 0),
        "duration": format_duration(seconds),
        "pace": _format_pace(seconds / (distance / 1000)) if distance > 0 else None,
        "grade_adjusted_pace": _format_pace(1000 / gap) if gap else None,
        "hr_avg": _positive_avg(samples, "directHeartRate"),
        "hr_max": max(hr_values) if hr_values else None,
        "cadence_spm": _positive_avg(samples, "directDoubleCadence"),
        "stride_cm": _positive_avg(samples, "directStrideLength"),
        "ground_contact_ms": _positive_avg(samples, "directGroundContactTime"),
        "vertical_oscillation_cm": _positive_avg(samples, "directVerticalOscillation", 1),
        "vertical_ratio_percent": _positive_avg(samples, "directVerticalRatio", 1),
        "power_w": _positive_avg(samples, "directPower"),
        "elevation_gain_m": round_number(gain, 0),
        "elevation_loss_m": round_number(loss, 0),
        "temperature_c": _positive_avg(samples, "directAirTemperature", 1),
        "performance_condition": _number(last.get("directPerformanceCondition")),
        "stamina_end_percent": _number(last.get("directPotentialStamina")),
        "_speed": distance / seconds if seconds > 0 else None,
    }


def _aerobic_decoupling(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compare speed per heartbeat between the first and second half (by distance)."""
    total = _number(rows[-1].get("sumDistance")) if rows else None
    if not total or total < 2000:
        return None

    def efficiency(samples: list[dict[str, Any]]) -> float | None:
        moving = [
            s for s in samples
            if (_number(s.get("directSpeed")) or 0) > 0.5
            and (_number(s.get("directHeartRate")) or 0) > 0
        ]
        if not moving:
            return None
        speed = sum(_number(s["directSpeed"]) for s in moving) / len(moving)
        hr = sum(_number(s["directHeartRate"]) for s in moving) / len(moving)
        return speed / hr

    first = efficiency([r for r in rows if _number(r["sumDistance"]) <= total / 2])
    second = efficiency([r for r in rows if _number(r["sumDistance"]) > total / 2])
    if not first or not second:
        return None
    percent = round_number((first - second) / first * 100, 1)
    if percent is None:
        return None
    if percent < 5:
        verdict = "excellent aerobic durability (<5%)"
    elif percent < 10:
        verdict = "moderate drift (5-10%)"
    else:
        verdict = "high drift (>10%): pace was above current aerobic fitness or conditions"
    return {"percent": percent, "interpretation": verdict}


def get_activity_distance_splits(
    activity_id: int,
    split_km: Annotated[
        float, Field(ge=0.2, le=10, description="Split length in kilometres.")
    ] = 1.0,
) -> dict[str, Any]:
    """Per-kilometre (or custom distance) splits computed from the second-by-second recording,
    regardless of the watch's auto-lap setting: pace, grade-adjusted pace, HR, cadence, stride,
    ground contact, vertical ratio, power, elevation, plus split pattern and aerobic decoupling."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint(
        f"activity_stream:{activity_id}",
        f"activity_details:{activity_id}",
        lambda: client.get_activity_details(activity_id, maxchart=10000, maxpoly=0),
        {},
        errors,
    )
    rows = _stream_rows(raw)
    if not rows:
        return {
            "activity_id": activity_id,
            "error": "no second-by-second data available for this activity",
            **_errors_payload(errors),
        }

    split_m = split_km * 1000
    splits: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    start_distance = start_time = 0.0
    boundary = split_m
    for row in rows:
        bucket.append(row)
        if _number(row["sumDistance"]) >= boundary:
            splits.append(_summarize_split(len(splits) + 1, bucket, start_distance, start_time))
            start_distance = _number(row["sumDistance"])
            start_time = _number(row["sumDuration"])
            boundary += split_m
            bucket = []
    if bucket and _number(bucket[-1]["sumDistance"]) - start_distance >= 10:
        partial = _summarize_split(len(splits) + 1, bucket, start_distance, start_time)
        partial["partial"] = True
        splits.append(partial)

    full = [s for s in splits if not s.get("partial") and s["_speed"]]
    pattern = None
    if len(full) >= 2:
        half = len(full) // 2
        first = sum(s["_speed"] for s in full[:half]) / half
        second = sum(s["_speed"] for s in full[-half:]) / half
        change = (second - first) / first * 100
        pattern = "negative" if change > 1 else "positive" if change < -1 else "even"
    fastest = max(full, key=lambda s: s["_speed"], default=None)
    slowest = min(full, key=lambda s: s["_speed"], default=None)
    for s in splits:
        s.pop("_speed", None)

    last = rows[-1]
    elapsed = _number(last.get("sumElapsedDuration"))
    timer = _number(last.get("sumDuration"))
    return {
        "activity_id": activity_id,
        "split_km": split_km,
        "split_count": len(splits),
        "splits": splits,
        "split_pattern": pattern,
        "fastest_split": fastest["split"] if fastest else None,
        "slowest_split": slowest["split"] if slowest else None,
        "aerobic_decoupling": _aerobic_decoupling(rows),
        "stopped_time": format_duration(elapsed - timer)
        if elapsed is not None and timer is not None and elapsed >= timer
        else None,
        **_errors_payload(errors),
    }


def _days_ago_iso(days: int) -> str:
    return (datetime.fromisoformat(today_iso()) - timedelta(days=days)).date().isoformat()


def _km(meters: Any) -> float | None:
    return round_number(meters / 1000, 2) if isinstance(meters, (int, float)) else None


def _fahrenheit_to_c(value: Any) -> float | None:
    return round_number((value - 32) * 5 / 9, 1) if isinstance(value, (int, float)) else None


def _shift_gmt_ms(value: Any, offset_ms: Any) -> str | None:
    """Shift a Garmin GMT string by a millisecond timezone offset into local time."""
    offset = offset_ms / 60000 if isinstance(offset_ms, (int, float)) else None
    return _gmt_to_local_iso(value, offset)


def _user_summary_day(day: str, errors: ErrorList | None = None) -> dict[str, Any]:
    client = _client()
    raw = _endpoint(
        f"user_summary:{day}",
        f"user_summary:{day}",
        lambda: client.get_user_summary(day),
        {},
        errors,
    )
    return raw if isinstance(raw, dict) else {}


def _user_summaries(days: int, errors: ErrorList) -> list[tuple[str, dict[str, Any]]]:
    dates = date_range_iso(days)
    return list(zip(dates, _fetch_days(dates, lambda d: _user_summary_day(d, errors)), strict=True))


def get_daily_activity(
    days: Annotated[int, Field(ge=1, le=30, description="Lookback window in days.")] = 7,
) -> dict[str, Any]:
    """Daily steps, distance, floors, calories, intensity minutes and active/sedentary time,
    plus weekly intensity minutes against the goal. Today is a partial day."""
    errors: ErrorList = []
    daily = []
    for day, s in _user_summaries(days, errors):
        floors = s.get("floorsAscended")
        daily.append(
            {
                "date": day,
                "steps": s.get("totalSteps"),
                "step_goal": s.get("dailyStepGoal"),
                "distance_km": _km(s.get("totalDistanceMeters")),
                "floors_climbed": round_number(floors, 1) if floors is not None else None,
                "floors_goal": s.get("userFloorsAscendedGoal"),
                "calories": {
                    "total": s.get("totalKilocalories"),
                    "active": s.get("activeKilocalories"),
                    "bmr": s.get("bmrKilocalories"),
                },
                "intensity_minutes": {
                    "moderate": s.get("moderateIntensityMinutes"),
                    "vigorous": s.get("vigorousIntensityMinutes"),
                },
                "activity_minutes": {
                    "highly_active": _minutes(s.get("highlyActiveSeconds")),
                    "active": _minutes(s.get("activeSeconds")),
                    "sedentary": _minutes(s.get("sedentarySeconds")),
                    "sleeping": _minutes(s.get("sleepingSeconds")),
                },
            }
        )

    client = _client()
    start, end = _days_ago_iso(27), today_iso()
    weekly_raw = _endpoint(
        f"weekly_intensity:{start}:{end}",
        f"weekly_intensity_minutes:{start}..{end}",
        lambda: client.get_weekly_intensity_minutes(start, end),
        [],
        errors,
    )
    weekly = []
    for week in weekly_raw if isinstance(weekly_raw, list) else []:
        if not isinstance(week, dict):
            continue
        moderate = week.get("moderateValue") or 0
        vigorous = week.get("vigorousValue") or 0
        weekly.append(
            {
                "week_start": week.get("calendarDate"),
                "moderate": moderate,
                "vigorous": vigorous,
                # Garmin counts vigorous minutes double toward the weekly goal.
                "total": moderate + 2 * vigorous,
                "goal": week.get("weeklyGoal"),
            }
        )

    steps = [d["steps"] for d in daily if isinstance(d["steps"], (int, float))]
    return {
        "period_days": len(daily),
        "total_steps": sum(steps) if steps else None,
        "avg_steps": _mean(steps, 0),
        "step_goal_days_met": sum(
            1
            for d in daily
            if isinstance(d["steps"], (int, float))
            and isinstance(d["step_goal"], (int, float))
            and d["steps"] >= d["step_goal"]
        ),
        "weekly_intensity_minutes": weekly,
        "daily": daily,
        **_errors_payload(errors),
    }


def _body_battery_event(event: dict[str, Any]) -> dict[str, Any]:
    duration = event.get("durationInMilliseconds")
    return {
        "type": event.get("eventType"),
        "start": _shift_gmt_ms(event.get("eventStartTimeGmt"), event.get("timezoneOffset")),
        "minutes": _minutes(duration / 1000) if isinstance(duration, (int, float)) else None,
        "impact": event.get("bodyBatteryImpact"),
        "feedback": event.get("shortFeedback"),
        "activity_name": event.get("activityName"),
        "activity_id": event.get("activityId"),
    }


def get_body_battery(
    days: Annotated[int, Field(ge=1, le=30, description="Lookback window in days.")] = 7,
) -> dict[str, Any]:
    """Daily Body Battery: charged, drained, highest, lowest, level at wake, sleep recharge,
    latest level, Garmin feedback, and the events (sleep, naps, activities) that moved it."""
    errors: ErrorList = []
    daily = []
    for day, s in _user_summaries(days, errors):
        feedback = s.get("bodyBatteryDynamicFeedbackEvent")
        events = s.get("bodyBatteryActivityEventList") or []
        daily.append(
            {
                "date": day,
                "charged": s.get("bodyBatteryChargedValue"),
                "drained": s.get("bodyBatteryDrainedValue"),
                "highest": s.get("bodyBatteryHighestValue"),
                "lowest": s.get("bodyBatteryLowestValue"),
                "at_wake": s.get("bodyBatteryAtWakeTime"),
                "sleep_recharge": s.get("bodyBatteryDuringSleep"),
                "latest": s.get("bodyBatteryMostRecentValue"),
                "feedback": feedback.get("feedbackLongType")
                if isinstance(feedback, dict)
                else None,
                "events": [_body_battery_event(e) for e in events if isinstance(e, dict)],
            }
        )
    return {
        "period_days": len(daily),
        "avg_at_wake": _mean([d["at_wake"] for d in daily], 0),
        "avg_sleep_recharge": _mean([d["sleep_recharge"] for d in daily], 0),
        "daily": daily,
        **_errors_payload(errors),
    }


def get_daily_vitals(
    days: Annotated[int, Field(ge=1, le=30, description="Lookback window in days.")] = 7,
) -> dict[str, Any]:
    """All-day vitals per day: heart rate (min/max/resting), SpO2, respiration, stress with
    time in rest/low/medium/high stress, and average altitude."""
    errors: ErrorList = []
    daily = []
    for day, s in _user_summaries(days, errors):
        daily.append(
            {
                "date": day,
                "heart_rate": {
                    "resting": s.get("restingHeartRate"),
                    "min": s.get("minHeartRate"),
                    "max": s.get("maxHeartRate"),
                    "resting_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate"),
                },
                "spo2": {
                    "avg": s.get("averageSpo2"),
                    "lowest": s.get("lowestSpo2"),
                    "latest": s.get("latestSpo2"),
                },
                "respiration": {
                    "waking_avg": s.get("avgWakingRespirationValue"),
                    "lowest": s.get("lowestRespirationValue"),
                    "highest": s.get("highestRespirationValue"),
                },
                "stress": {
                    "avg": s.get("averageStressLevel"),
                    "max": s.get("maxStressLevel"),
                    "rest_minutes": _minutes(s.get("restStressDuration")),
                    "low_minutes": _minutes(s.get("lowStressDuration")),
                    "medium_minutes": _minutes(s.get("mediumStressDuration")),
                    "high_minutes": _minutes(s.get("highStressDuration")),
                    "activity_minutes": _minutes(s.get("activityStressDuration")),
                },
                "avg_altitude_m": s.get("averageMonitoringEnvironmentAltitude"),
            }
        )
    return {
        "period_days": len(daily),
        "avg_resting_hr": _mean([d["heart_rate"]["resting"] for d in daily]),
        "avg_spo2": _mean([d["spo2"]["avg"] for d in daily]),
        "avg_waking_respiration": _mean([d["respiration"]["waking_avg"] for d in daily]),
        "avg_stress": _mean([d["stress"]["avg"] for d in daily]),
        "daily": daily,
        **_errors_payload(errors),
    }


def _readiness_factor(item: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {
        "percent": item.get(f"{prefix}FactorPercent"),
        "feedback": item.get(f"{prefix}FactorFeedback"),
    }


def get_training_readiness_history(
    days: Annotated[int, Field(ge=1, le=30, description="Lookback window in days.")] = 7,
) -> dict[str, Any]:
    """Daily training readiness score and level with the contribution of each factor:
    sleep, sleep history, recovery time, acute load (ACWR), stress history and HRV."""
    errors: ErrorList = []
    dates = date_range_iso(days)
    daily = []
    for day, raw in zip(dates, _fetch_days(dates, lambda d: _training_readiness(d, errors)),
                        strict=True):
        items = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
        if isinstance(raw, dict):
            items = [raw]
        if not items:
            daily.append({"date": day, "score": None})
            continue
        item = max(items, key=lambda i: str(i.get("timestamp") or ""))
        recovery_minutes = item.get("recoveryTime")
        daily.append(
            {
                "date": day,
                "score": item.get("score"),
                "level": item.get("level"),
                "feedback": item.get("feedbackShort"),
                "updated": item.get("timestampLocal"),
                "factors": {
                    "sleep": {**_readiness_factor(item, "sleepScore"),
                              "sleep_score": item.get("sleepScore")},
                    "sleep_history": _readiness_factor(item, "sleepHistory"),
                    "recovery_time": {
                        **_readiness_factor(item, "recoveryTime"),
                        "hours": round_number(recovery_minutes / 60, 1)
                        if isinstance(recovery_minutes, (int, float))
                        else None,
                    },
                    "acute_load": {**_readiness_factor(item, "acwr"),
                                   "acute_load": item.get("acuteLoad")},
                    "stress_history": _readiness_factor(item, "stressHistory"),
                    "hrv": {**_readiness_factor(item, "hrv"),
                            "weekly_avg_ms": item.get("hrvWeeklyAverage")},
                },
            }
        )
    return {
        "period_days": len(daily),
        "avg_score": _mean([d["score"] for d in daily]),
        "daily": daily,
        **_errors_payload(errors),
    }


_ENDURANCE_LEVELS = (
    ("ELITE", "classificationLowerLimitElite"),
    ("SUPERIOR", "classificationLowerLimitSuperior"),
    ("EXPERT", "classificationLowerLimitExpert"),
    ("WELL_TRAINED", "classificationLowerLimitWellTrained"),
    ("TRAINED", "classificationLowerLimitTrained"),
    ("INTERMEDIATE", "classificationLowerLimitIntermediate"),
)


def _endurance_section(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    dto = raw.get("enduranceScoreDTO") or {}
    score = dto.get("overallScore")
    level, next_level = None, None
    if isinstance(score, (int, float)):
        for name, key in _ENDURANCE_LEVELS:
            limit = dto.get(key)
            if not isinstance(limit, (int, float)):
                continue
            if score >= limit:
                level = name
                break
            next_level = {"level": name, "points_needed": limit - score}
        level = level or "RECREATIONAL"
    group_map = raw.get("groupMap") or {}
    return {
        "date": dto.get("calendarDate"),
        "score": score,
        "level": level,
        "next_level": next_level,
        "period_avg": raw.get("avg"),
        "period_max": raw.get("max"),
        "weekly": [
            {"week_start": week, "avg": vals.get("groupAverage"), "max": vals.get("groupMax")}
            for week, vals in sorted(group_map.items())
            if isinstance(vals, dict)
        ],
    }


def _hill_section(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    scored = [
        d for d in raw.get("hillScoreDTOList") or []
        if isinstance(d, dict) and d.get("overallScore") is not None
    ]
    latest = max(scored, key=lambda d: str(d.get("calendarDate")), default={})
    return {
        "date": latest.get("calendarDate"),
        "score": latest.get("overallScore"),
        "strength": latest.get("strengthScore"),
        "endurance": latest.get("enduranceScore"),
        "period_max": raw.get("maxScore"),
    }


def _fitness_age_section(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    components = raw.get("components") or {}

    def component(name: str) -> dict[str, Any] | None:
        item = components.get(name)
        if not isinstance(item, dict):
            return None
        return {
            "value": item.get("value"),
            "target": item.get("targetValue"),
            # Garmin ranks which component to improve first (1 = highest impact).
            "improvement_priority": item.get("priority"),
        }

    return {
        "chronological_age": raw.get("chronologicalAge"),
        "fitness_age": round_number(raw.get("fitnessAge"), 1),
        "achievable_fitness_age": round_number(raw.get("achievableFitnessAge"), 1),
        "components": {
            "vigorous_days_per_week": component("vigorousDaysAvg"),
            "vigorous_minutes_per_week": component("vigorousMinutesAvg"),
            "resting_hr": component("rhr"),
            "bmi": component("bmi"),
        },
        "updated": raw.get("lastUpdated"),
    }


def _lactate_section(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    shr = raw.get("speed_and_heart_rate") or {}
    power = raw.get("power") or {}
    speed = shr.get("speed")
    # Garmin stores lactate threshold speed in units of 10 m/s.
    pace = _format_pace(1000 / (speed * 10)) if isinstance(speed, (int, float)) and speed else None
    return {
        "date": str(shr.get("calendarDate") or "")[:10] or None,
        "heart_rate": shr.get("heartRate"),
        "pace_per_km": pace,
        "running_ftp_w": power.get("functionalThresholdPower"),
        "power_to_weight": round_number(power.get("powerToWeight"), 2),
        "weight_kg": power.get("weight"),
    }


def get_performance_metrics() -> dict[str, Any]:
    """Endurance score and level, hill score, fitness age, lactate threshold (HR, pace,
    running power), heat/altitude acclimation, and VO2 max trend over the last 30 days."""
    client = _client()
    errors: ErrorList = []
    start, end = _days_ago_iso(29), today_iso()

    def fetch(name: str, factory: Callable[[], Any], default: Any = None) -> Any:
        return _endpoint(f"{name}:{start}:{end}", f"{name}:{start}..{end}", factory, default,
                         errors)

    endurance = fetch("endurance_score", lambda: client.get_endurance_score(start, end))
    hill = fetch("hill_score", lambda: client.get_hill_score(start, end))
    fitness_age = fetch("fitness_age", lambda: client.get_fitnessage_data(end))
    lactate = fetch("lactate_threshold", lambda: client.get_lactate_threshold())
    max_metrics = fetch("max_metrics_range", lambda: client.get_max_metrics_range(start, end), [])

    entries = [m for m in max_metrics or [] if isinstance(m, dict)]
    vo2_trend = []
    acclimation = None
    for entry in entries:
        generic = entry.get("generic") or {}
        if generic.get("vo2MaxPreciseValue") is not None:
            vo2_trend.append(
                {"date": generic.get("calendarDate"), "vo2_max": generic.get("vo2MaxPreciseValue")}
            )
        heat = entry.get("heatAltitudeAcclimation")
        if isinstance(heat, dict):
            acclimation = {
                "date": heat.get("calendarDate"),
                "heat_percent": heat.get("heatAcclimationPercentage"),
                "heat_trend": heat.get("heatTrend"),
                "altitude_acclimation_m": heat.get("altitudeAcclimation"),
                "altitude_trend": heat.get("altitudeTrend"),
                "current_altitude_m": heat.get("currentAltitude"),
            }
    vo2_trend.sort(key=lambda v: str(v["date"]))

    return {
        "endurance_score": _endurance_section(endurance),
        "hill_score": _hill_section(hill),
        "fitness_age": _fitness_age_section(fitness_age),
        "lactate_threshold": _lactate_section(lactate),
        "acclimation": acclimation,
        "vo2_max_trend": vo2_trend,
        **_errors_payload(errors),
    }


def get_race_prediction_trend(
    days: Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")] = 30,
) -> dict[str, Any]:
    """Daily Garmin race predictions (5K, 10K, half, marathon) and how they changed."""
    client = _client()
    errors: ErrorList = []
    start, end = _days_ago_iso(days - 1), today_iso()
    raw = _endpoint(
        f"race_predictions:{start}:{end}",
        f"race_predictions:{start}..{end}",
        lambda: client.get_race_predictions(start, end, "daily"),
        [],
        errors,
    )
    keys = (("5k", "time5K"), ("10k", "time10K"), ("half_marathon", "timeHalfMarathon"),
            ("marathon", "timeMarathon"))
    rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    rows.sort(key=lambda r: str(r.get("calendarDate")))
    daily = [
        {"date": r.get("calendarDate"),
         **{name: format_duration(r.get(key)) for name, key in keys}}
        for r in rows
    ]
    change = None
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        change = {
            name: last[key] - first[key]
            for name, key in keys
            if isinstance(first.get(key), (int, float)) and isinstance(last.get(key), (int, float))
        }
    return {
        "period_days": days,
        "latest": daily[-1] if daily else None,
        "change_seconds": change,
        "daily": daily,
        **_errors_payload(errors),
    }


def get_running_tolerance(
    weeks: Annotated[int, Field(ge=1, le=26, description="Lookback window in weeks.")] = 8,
) -> dict[str, Any]:
    """Weekly running impact load against Garmin's running tolerance (injury-risk guide)."""
    client = _client()
    errors: ErrorList = []
    start, end = _days_ago_iso(weeks * 7 - 1), today_iso()
    raw = _endpoint(
        f"running_tolerance:{start}:{end}",
        f"running_tolerance:{start}..{end}",
        lambda: client.get_running_tolerance(start, end),
        [],
        errors,
    )
    rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    rows.sort(key=lambda r: str(r.get("startOfWeek")))
    weekly = []
    for r in rows:
        load, tolerance = r.get("totalImpactLoad"), r.get("tolerance")
        weekly.append(
            {
                "week_start": r.get("startOfWeek"),
                "week_end": r.get("endOfWeek"),
                "impact_load": load,
                "tolerance": tolerance,
                "percent_of_tolerance": round_number(load / tolerance * 100, 0)
                if isinstance(load, (int, float)) and isinstance(tolerance, (int, float))
                and tolerance
                else None,
                "distance_km": _km(r.get("totalDistance")),
            }
        )
    return {"weeks": len(weekly), "weekly": weekly, **_errors_payload(errors)}


def get_gear() -> dict[str, Any]:
    """Shoes and other gear: total distance, activity count, percent of max distance and
    km remaining, default activity types, last use, and last-30-day usage."""
    client = _client()
    errors: ErrorList = []
    profile = _endpoint("user_profile", "user_profile", lambda: client.get_user_profile(), {},
                        errors)
    profile_id = profile.get("id") if isinstance(profile, dict) else None
    if profile_id is None:
        return {"count": 0, "gear": [], "error": "user profile unavailable",
                **_errors_payload(errors)}
    raw = _endpoint(f"gear:{profile_id}", "gear", lambda: client.get_gear(profile_id), [], errors)
    items = [g for g in raw if isinstance(g, dict)] if isinstance(raw, list) else []

    def stats(uuid: Any) -> dict[str, Any]:
        result = _endpoint(f"gear_stats:{uuid}", f"gear_stats:{uuid}",
                           lambda: client.get_gear_stats(uuid), {}, errors)
        return result if isinstance(result, dict) else {}

    def activities(uuid: Any) -> list[dict[str, Any]]:
        result = _endpoint(f"gear_activities:{uuid}", f"gear_activities:{uuid}",
                           lambda: client.get_gear_activities(uuid, 1000), [], errors)
        return [a for a in result if isinstance(a, dict)] if isinstance(result, list) else []

    defaults_raw = _endpoint(f"gear_defaults:{profile_id}", "gear_defaults",
                             lambda: client.get_gear_defaults(profile_id), [], errors)
    default_for: dict[Any, list[Any]] = defaultdict(list)
    for d in defaults_raw if isinstance(defaults_raw, list) else []:
        if isinstance(d, dict) and d.get("uuid"):
            default_for[d["uuid"]].append(d.get("activityTypeName") or d.get("activityTypePk"))
    since = _days_ago_iso(29)
    all_stats = _fetch_days([g.get("uuid") for g in items], stats)
    all_activities = _fetch_days([g.get("uuid") for g in items], activities)
    gear = []
    for item, stat, acts in zip(items, all_stats, all_activities, strict=True):
        distance, maximum = stat.get("totalDistance"), item.get("maximumMeters")
        acts = sorted(acts, key=lambda a: str(a.get("startTimeLocal")), reverse=True)
        recent = [a for a in acts if str(a.get("startTimeLocal") or "")[:10] >= since]
        last_use = acts[0] if acts else None
        gear.append(
            {
                "name": item.get("displayName") or item.get("customMakeModel")
                or item.get("gearModelName"),
                "model": item.get("customMakeModel") or item.get("gearModelName"),
                "type": item.get("gearTypeName"),
                "status": item.get("gearStatusName"),
                "in_use_since": str(item.get("dateBegin") or "")[:10] or None,
                "retired_on": str(item.get("dateEnd") or "")[:10] or None,
                "distance_km": round_number(distance / 1000, 1)
                if isinstance(distance, (int, float))
                else None,
                "activities": stat.get("totalActivities"),
                "max_distance_km": round_number(maximum / 1000, 0)
                if isinstance(maximum, (int, float))
                else None,
                "percent_used": round_number(distance / maximum * 100, 0)
                if isinstance(distance, (int, float)) and isinstance(maximum, (int, float))
                and maximum
                else None,
                "remaining_km": round_number((maximum - distance) / 1000, 0)
                if isinstance(distance, (int, float)) and isinstance(maximum, (int, float))
                else None,
                "default_for": default_for.get(item.get("uuid")) or [],
                "last_used": {
                    "date": str(last_use.get("startTimeLocal") or "")[:10] or None,
                    "activity": last_use.get("activityName"),
                    "distance_km": _km(last_use.get("distance")),
                }
                if last_use
                else None,
                "last_30_days": {
                    "activities": len(recent),
                    "distance_km": round_number(
                        sum(_number(a.get("distance")) or 0 for a in recent) / 1000, 1
                    ),
                },
            }
        )
    gear.sort(key=lambda g: g["status"] != "active")
    return {"count": len(gear), "gear": gear, **_errors_payload(errors)}


def get_devices() -> dict[str, Any]:
    """Registered Garmin devices with firmware version, last upload time, and alarms."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint("devices", "devices", lambda: client.get_devices(), [], errors)
    last = _endpoint("device_last_used", "device_last_used",
                     lambda: client.get_device_last_used(), {}, errors)
    last = last if isinstance(last, dict) else {}
    upload = last.get("lastUsedDeviceUploadTime")
    alarms_raw = _endpoint("device_alarms", "device_alarms",
                           lambda: client.get_device_alarms(), [], errors)
    alarms = [
        {
            "time": _clock_from_minutes(a.get("alarmTime")),
            "days": a.get("alarmDays"),
            "on": a.get("alarmMode") == "ON",
            "sound": a.get("alarmSound"),
        }
        for a in (alarms_raw if isinstance(alarms_raw, list) else [])
        if isinstance(a, dict)
    ]
    devices = [
        {
            "name": d.get("productDisplayName"),
            "nickname": d.get("displayName"),
            "device_id": d.get("deviceId"),
            "firmware": d.get("currentFirmwareVersion"),
            "primary_tracker": d.get("primaryActivityTrackerIndicator"),
        }
        for d in (raw if isinstance(raw, list) else [])
        if isinstance(d, dict)
    ]
    return {
        "count": len(devices),
        "devices": devices,
        "last_used": {
            "name": last.get("lastUsedDeviceName"),
            "last_upload_utc": datetime.fromtimestamp(upload / 1000, tz=UTC)
            .isoformat(timespec="minutes")
            if isinstance(upload, (int, float))
            else None,
        },
        "alarms": alarms,
        **_errors_payload(errors),
    }


def _grams_to_kg(value: Any) -> float | None:
    return round_number(value / 1000, 1) if isinstance(value, (int, float)) else None


def get_body_metrics(
    days: Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")] = 90,
) -> dict[str, Any]:
    """Logged weigh-ins and body composition, blood pressure readings, and today's hydration.
    Empty lists mean nothing was logged in Garmin Connect for the period."""
    client = _client()
    errors: ErrorList = []
    start, end = _days_ago_iso(days - 1), today_iso()
    weigh_raw = _endpoint(f"weigh_ins:{start}:{end}", f"weigh_ins:{start}..{end}",
                          lambda: client.get_weigh_ins(start, end), {}, errors)
    bp_raw = _endpoint(f"blood_pressure:{start}:{end}", f"blood_pressure:{start}..{end}",
                       lambda: client.get_blood_pressure(start, end), {}, errors)
    hydration = _endpoint(f"hydration:{end}", f"hydration:{end}",
                          lambda: client.get_hydration_data(end), {}, errors)

    weigh_ins = []
    for summary in (weigh_raw or {}).get("dailyWeightSummaries") or []:
        latest = summary.get("latestWeight") if isinstance(summary, dict) else None
        if not isinstance(latest, dict):
            continue
        weigh_ins.append(
            {
                "date": summary.get("summaryDate") or latest.get("calendarDate"),
                "weight_kg": _grams_to_kg(latest.get("weight")),
                "bmi": round_number(latest.get("bmi"), 1),
                "body_fat_percent": latest.get("bodyFat"),
                "body_water_percent": latest.get("bodyWater"),
                "muscle_mass_kg": _grams_to_kg(latest.get("muscleMass")),
                "bone_mass_kg": _grams_to_kg(latest.get("boneMass")),
                "visceral_fat": latest.get("visceralFat"),
                "metabolic_age": latest.get("metabolicAge"),
            }
        )

    readings = []
    for summary in (bp_raw or {}).get("measurementSummaries") or []:
        for m in (summary.get("measurements") or []) if isinstance(summary, dict) else []:
            if isinstance(m, dict):
                readings.append(
                    {
                        "time": m.get("measurementTimestampLocal"),
                        "systolic": m.get("systolic"),
                        "diastolic": m.get("diastolic"),
                        "pulse": m.get("pulse"),
                        "category": m.get("categoryName") or m.get("category"),
                    }
                )

    hydration = hydration if isinstance(hydration, dict) else {}
    return {
        "period_days": days,
        "weigh_ins": weigh_ins,
        "blood_pressure": readings,
        "hydration_today": {
            "intake_ml": hydration.get("valueInML"),
            "goal_ml": hydration.get("goalInML"),
            "sweat_loss_ml": hydration.get("sweatLossInML"),
        },
        **_errors_payload(errors),
    }


def _resolve_date(value: str | None) -> str:
    """Return an ISO date; empty means today. Raises ValueError for malformed input."""
    if not value:
        return today_iso()
    return datetime.fromisoformat(value.strip()[:10]).date().isoformat()


def _offset_ms(gmt: Any, local: Any) -> int:
    """Timezone offset in ms from a pair of Garmin GMT/local timestamps (strings or epoch ms)."""
    if isinstance(gmt, (int, float)) and isinstance(local, (int, float)):
        return int(local - gmt)
    try:
        g = datetime.fromisoformat(str(gmt).replace(" ", "T").split(".")[0])
        lo = datetime.fromisoformat(str(local).replace(" ", "T").split(".")[0])
    except ValueError:
        return 0
    return int((lo - g).total_seconds() * 1000)


def _epoch_local(ms: Any, offset_ms: int) -> datetime | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp((ms + offset_ms) / 1000, tz=UTC).replace(tzinfo=None)


def _iso_minutes(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="minutes") if moment else None


def _pairs(values: Any, value_index: int = 1) -> list[tuple[Any, Any]]:
    """[[timestamp_ms, value, ...], ...] -> [(timestamp_ms, value)] with non-null values."""
    out = []
    for item in values or []:
        if isinstance(item, (list, tuple)) and len(item) > value_index:
            if item[0] is not None and item[value_index] is not None:
                out.append((item[0], item[value_index]))
    return out


def _hourly(
    points: list[tuple[Any, Any]], offset_ms: int, valid: Callable[[Any], bool]
) -> dict[str, list[float]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for ts, value in points:
        moment = _epoch_local(ts, offset_ms)
        if moment is not None and valid(value):
            buckets[moment.strftime("%H:00")].append(float(value))
    return buckets


def _zone_ranges(floors: list[Any], top: Any) -> list[dict[str, Any]]:
    zones = []
    for i, low in enumerate(floors):
        if not isinstance(low, (int, float)) or low <= 0:
            continue
        nxt = floors[i + 1] if i + 1 < len(floors) else None
        high = nxt - 1 if isinstance(nxt, (int, float)) and nxt > 0 else top
        zones.append({"zone": i + 1, "low": round_number(low, 0), "high": round_number(high, 0)
                      if isinstance(high, (int, float)) else None})
    return zones


def _hr_zone_sets(raw: Any) -> list[dict[str, Any]]:
    sets = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        floors = [item.get(f"zone{i}Floor") for i in range(1, 6)]
        sets.append(
            {
                "sport": item.get("sport"),
                "method": item.get("trainingMethod"),
                "max_hr": item.get("maxHeartRateUsed"),
                "resting_hr": item.get("restingHeartRateUsed"),
                "lactate_threshold_hr": item.get("lactateThresholdHeartRateUsed"),
                "zones_bpm": _zone_ranges(floors, item.get("maxHeartRateUsed")),
            }
        )
    return sets


def _power_zone_set(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    floors = [raw.get(f"zone{i}Floor") for i in range(1, 8)]
    return {
        "sport": raw.get("sport"),
        "threshold_power_w": raw.get("functionalThresholdPower"),
        "zones_w": _zone_ranges(floors, None),
    }


def _week_buckets(daily: list[tuple[str, Any]], weeks: int) -> list[dict[str, Any]]:
    """Average (date, value) pairs into 7-day buckets ending today, oldest first."""
    today = datetime.fromisoformat(today_iso()).date()
    buckets: dict[int, list[float]] = defaultdict(list)
    for day, value in daily:
        if not isinstance(value, (int, float)):
            continue
        try:
            age = (today - datetime.fromisoformat(str(day)[:10]).date()).days
        except ValueError:
            continue
        if 0 <= age < weeks * 7:
            buckets[age // 7].append(float(value))
    rows = []
    for index in sorted(buckets, reverse=True):
        start = today - timedelta(days=index * 7 + 6)
        values = buckets[index]
        rows.append(
            {
                "week_start": start.isoformat(),
                "avg": round_number(sum(values) / len(values), 1),
                "days": len(values),
            }
        )
    return rows


def _trend(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = [r["avg"] for r in rows if isinstance(r.get("avg"), (int, float))]
    if len(values) < 2:
        return None
    recent = values[-4:]
    earlier = values[:-4] or values[:1]
    return {
        "latest_week": values[-1],
        "last_4_weeks_avg": round_number(sum(recent) / len(recent), 1),
        "earlier_avg": round_number(sum(earlier) / len(earlier), 1),
    }


def get_long_term_trends(
    weeks: Annotated[int, Field(ge=2, le=52, description="Lookback window in weeks.")] = 12,
) -> dict[str, Any]:
    """Weekly trends over up to 52 weeks: steps, stress, overnight HRV, resting HR, sleep
    duration and score, overnight SpO2 and respiration, with recent-vs-earlier comparisons."""
    client = _client()
    errors: ErrorList = []
    end = today_iso()
    # Garmin's daily range endpoints serve at most ~90 days per call.
    start = _days_ago_iso(min(weeks * 7, 90) - 1)

    steps_raw = _endpoint(f"weekly_steps:{end}:{weeks}", f"weekly_steps:{weeks}w",
                          lambda: client.get_weekly_steps(end, weeks), [], errors)
    stress_raw = _endpoint(f"weekly_stress:{end}:{weeks}", f"weekly_stress:{weeks}w",
                           lambda: client.get_weekly_stress(end, weeks), [], errors)
    hrv_raw = _endpoint(f"hrv_range:{start}:{end}", f"hrv_range:{start}..{end}",
                        lambda: client.get_hrv_data_range(start, end), {}, errors)
    rhr_raw = _endpoint(f"rhr_range:{start}:{end}", f"rhr_range:{start}..{end}",
                        lambda: client.get_rhr_daily(start, end), [], errors)
    sleep_raw = _endpoint(f"sleep_range:{start}:{end}", f"sleep_range:{start}..{end}",
                          lambda: client.get_sleep_daily(start, end), [], errors)

    weekly_steps = [
        {
            "week_start": w.get("calendarDate"),
            "total_steps": round_number((w.get("values") or {}).get("totalSteps"), 0),
            "avg_steps": round_number((w.get("values") or {}).get("averageSteps"), 0),
            "total_km": _km((w.get("values") or {}).get("totalDistance")),
        }
        for w in (steps_raw if isinstance(steps_raw, list) else [])
        if isinstance(w, dict)
    ]
    weekly_stress = [
        {"week_start": w.get("calendarDate"), "avg": w.get("value")}
        for w in (stress_raw if isinstance(stress_raw, list) else [])
        if isinstance(w, dict)
    ]

    hrv_items = [h for h in (hrv_raw or {}).get("hrvSummaries") or [] if isinstance(h, dict)]
    hrv_weekly = _week_buckets([(h.get("calendarDate"), h.get("lastNightAvg"))
                                for h in hrv_items], weeks)
    latest_hrv = max(hrv_items, key=lambda h: str(h.get("calendarDate")), default={})
    baseline = latest_hrv.get("baseline") or {}

    rhr_weekly = _week_buckets([(r.get("calendarDate"), r.get("value"))
                                for r in (rhr_raw if isinstance(rhr_raw, list) else [])
                                if isinstance(r, dict)], weeks)

    sleep_items = [(s.get("calendarDate"), s.get("values") or {})
                   for s in (sleep_raw if isinstance(sleep_raw, list) else [])
                   if isinstance(s, dict)]

    def sleep_series(key: str, scale: float = 1.0) -> list[dict[str, Any]]:
        return _week_buckets(
            [(day, v.get(key) / scale if isinstance(v.get(key), (int, float)) else None)
             for day, v in sleep_items],
            weeks,
        )

    sleep_hours = sleep_series("totalSleepTimeInSeconds", 3600)
    sleep_score = sleep_series("sleepScore")
    return {
        "weeks": weeks,
        "daily_series_days": min(weeks * 7, 90),
        "steps": {"weekly": weekly_steps},
        "stress": {"weekly": weekly_stress, "trend": _trend(weekly_stress)},
        "hrv": {
            "weekly": hrv_weekly,
            "trend": _trend(hrv_weekly),
            "latest_status": latest_hrv.get("status"),
            "baseline_balanced_range_ms": [baseline.get("balancedLow"),
                                           baseline.get("balancedUpper")]
            if baseline else None,
        },
        "resting_hr": {"weekly": rhr_weekly, "trend": _trend(rhr_weekly)},
        "sleep": {
            "hours_weekly": sleep_hours,
            "hours_trend": _trend(sleep_hours),
            "score_weekly": sleep_score,
            "score_trend": _trend(sleep_score),
            "spo2_weekly": sleep_series("spO2"),
            "respiration_weekly": sleep_series("respiration"),
        },
        **_errors_payload(errors),
    }


def get_hrv_trend(
    days: Annotated[int, Field(ge=7, le=90, description="Lookback window in days.")] = 30,
) -> dict[str, Any]:
    """Daily overnight HRV for up to 90 days with 5-minute high, 7-day average, status, and
    your personal baseline (balanced range), plus how many nights fell below baseline."""
    client = _client()
    errors: ErrorList = []
    start, end = _days_ago_iso(days - 1), today_iso()
    raw = _endpoint(f"hrv_range:{start}:{end}", f"hrv_range:{start}..{end}",
                    lambda: client.get_hrv_data_range(start, end), {}, errors)
    items = sorted(
        (h for h in (raw or {}).get("hrvSummaries") or [] if isinstance(h, dict)),
        key=lambda h: str(h.get("calendarDate")),
    )
    daily = []
    below = 0
    for h in items:
        base = h.get("baseline") or {}
        night = h.get("lastNightAvg")
        low = base.get("balancedLow")
        if isinstance(night, (int, float)) and isinstance(low, (int, float)) and night < low:
            below += 1
        daily.append(
            {
                "date": h.get("calendarDate"),
                "last_night_avg_ms": night,
                "five_min_high_ms": h.get("lastNight5MinHigh"),
                "weekly_avg_ms": h.get("weeklyAvg"),
                "status": h.get("status"),
                "baseline_range_ms": [low, base.get("balancedUpper")] if base else None,
            }
        )
    nights = [d["last_night_avg_ms"] for d in daily]
    latest = daily[-1] if daily else {}
    return {
        "period_days": days,
        "nights": len(daily),
        "avg_ms": _mean(nights),
        "min_ms": min((n for n in nights if isinstance(n, (int, float))), default=None),
        "max_ms": max((n for n in nights if isinstance(n, (int, float))), default=None),
        "nights_below_baseline": below,
        "latest": latest or None,
        "daily": daily,
        **_errors_payload(errors),
    }


_SEGMENT_TYPES = {"RWD_RUN": "run", "RWD_WALK": "walk", "RWD_STAND": "stand"}


def get_activity_segments(activity_id: int) -> dict[str, Any]:
    """Garmin's run/walk/stand detection for one activity: each segment's start time,
    distance, duration, pace and HR, totals per segment type, and paused time."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint(f"typed_splits:{activity_id}", f"typed_splits:{activity_id}",
                    lambda: client.get_activity_typed_splits(activity_id), {}, errors)
    splits = [s for s in (raw or {}).get("splits") or [] if isinstance(s, dict)]
    if not splits:
        return {"activity_id": activity_id, "error": "no segment data for this activity",
                **_errors_payload(errors)}

    segments = []
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"seconds": 0.0, "meters": 0.0,
                                                               "count": 0})
    active = next((s for s in splits if s.get("type") == "INTERVAL_ACTIVE"), None)
    for s in splits:
        kind = _SEGMENT_TYPES.get(str(s.get("type")))
        seconds = _number(s.get("duration")) or 0
        if kind is None or seconds <= 0:
            continue
        meters = _number(s.get("distance")) or 0
        totals[kind]["seconds"] += seconds
        totals[kind]["meters"] += meters
        totals[kind]["count"] += 1
        segments.append(
            {
                "type": kind,
                "start": str(s.get("startTimeLocal") or "")[:19] or None,
                "duration": format_duration(seconds),
                "distance_m": round_number(meters, 0),
                "pace": _format_pace(seconds / (meters / 1000)) if meters > 0 else None,
                "hr_avg": round_number(s.get("averageHR"), 0),
                "cadence_spm": round_number(s.get("averageRunCadence"), 0),
            }
        )
    segments.sort(key=lambda seg: seg["start"] or "")
    paused = None
    if active:
        elapsed, timer = _number(active.get("elapsedDuration")), _number(active.get("duration"))
        if elapsed is not None and timer is not None and elapsed >= timer:
            paused = format_duration(elapsed - timer)
    return {
        "activity_id": activity_id,
        "segment_count": len(segments),
        "totals": {
            kind: {"count": int(t["count"]), "duration": format_duration(t["seconds"]),
                   "distance_km": round_number(t["meters"] / 1000, 2)}
            for kind, t in totals.items()
        },
        "paused_time": paused,
        "segments": segments,
        **_errors_payload(errors),
    }


def get_daily_timeline(
    date: Annotated[str, Field(description="Day as YYYY-MM-DD; empty for today.")] = "",
) -> dict[str, Any]:
    """Hour-by-hour view of one day: heart rate (min/avg/max), stress, Body Battery,
    SpO2 and respiration, plus activities Garmin auto-detected during the day."""
    try:
        day = _resolve_date(date)
    except ValueError:
        return {"error": f"invalid date: {date!r}; use YYYY-MM-DD"}
    client = _client()
    errors: ErrorList = []

    def fetch(name: str, factory: Callable[[], Any], default: Any) -> Any:
        result = _endpoint(f"{name}:{day}", f"{name}:{day}", factory, default, errors)
        return result if isinstance(result, type(default)) else default

    hr = fetch("heart_rates", lambda: client.get_heart_rates(day), {})
    stress = fetch("all_day_stress", lambda: client.get_all_day_stress(day), {})
    spo2 = fetch("spo2", lambda: client.get_spo2_data(day), {})
    resp = fetch("respiration", lambda: client.get_respiration_data(day), {})
    events = fetch("all_day_events", lambda: client.get_all_day_events(day), [])

    source = next((r for r in (hr, stress, spo2, resp) if r.get("startTimestampGMT")), {})
    offset = _offset_ms(source.get("startTimestampGMT"), source.get("startTimestampLocal"))

    hr_h = _hourly(_pairs(hr.get("heartRateValues")), offset, lambda v: v > 0)
    stress_h = _hourly(_pairs(stress.get("stressValuesArray")), offset, lambda v: 0 <= v <= 100)
    bb_h = _hourly(_pairs(stress.get("bodyBatteryValuesArray"), 2), offset, lambda v: v >= 0)
    spo2_h = _hourly(_pairs(spo2.get("spO2HourlyAverages")), offset, lambda v: v > 0)
    resp_h = _hourly(_pairs(resp.get("respirationAveragesValuesArray")), offset,
                     lambda v: v > 0)

    hours = sorted(set(hr_h) | set(stress_h) | set(bb_h) | set(spo2_h) | set(resp_h))
    timeline = []
    for hour in hours:
        hrs = hr_h.get(hour, [])
        timeline.append(
            {
                "hour": hour,
                "hr_min": round_number(min(hrs), 0) if hrs else None,
                "hr_avg": round_number(sum(hrs) / len(hrs), 0) if hrs else None,
                "hr_max": round_number(max(hrs), 0) if hrs else None,
                "stress_avg": _mean(stress_h.get(hour, []), 0),
                "body_battery_end": round_number(bb_h[hour][-1], 0) if bb_h.get(hour) else None,
                "spo2_avg": _mean(spo2_h.get(hour, []), 0),
                "respiration_avg": _mean(resp_h.get(hour, []), 1),
            }
        )
    detected = [
        {
            "type": e.get("activityType"),
            "start": str(e.get("startTimestampLocal") or "")[:16] or None,
            "end": str(e.get("endTimestampLocal") or "")[:16] or None,
            "minutes": e.get("duration"),
            "intensity_minutes": {"moderate": e.get("moderateIntensityMinutes"),
                                  "vigorous": e.get("vigorousIntensityMinutes")},
        }
        for e in events
        if isinstance(e, dict)
    ]
    return {
        "date": day,
        "summary": {
            "hr_min": hr.get("minHeartRate"),
            "hr_max": hr.get("maxHeartRate"),
            "resting_hr": hr.get("restingHeartRate"),
            "stress_avg": stress.get("avgStressLevel"),
            "stress_max": stress.get("maxStressLevel"),
            "spo2_avg": spo2.get("averageSpO2"),
            "spo2_lowest": spo2.get("lowestSpO2"),
            "spo2_7d_avg": round_number(spo2.get("lastSevenDaysAvgSpO2"), 1),
            "respiration_waking_avg": resp.get("avgWakingRespirationValue"),
        },
        "hourly": timeline,
        "auto_detected_activities": detected,
        **_errors_payload(errors),
    }


_SLEEP_STAGES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def get_sleep_detail(
    date: Annotated[str, Field(description="Wake-up day as YYYY-MM-DD; empty for today.")] = "",
) -> dict[str, Any]:
    """One night in detail: stage-by-stage timeline (deep/light/REM/awake with clock times),
    hour-by-hour HR, HRV, SpO2, stress and Body Battery, SpO2 time below 90%, restless
    moments and breathing disruptions."""
    try:
        day = _resolve_date(date)
    except ValueError:
        return {"error": f"invalid date: {date!r}; use YYYY-MM-DD"}
    errors: ErrorList = []
    raw = _sleep_day(day, errors)
    dto = raw.get("dailySleepDTO") or {}
    if not dto.get("sleepTimeSeconds"):
        return {"date": day, "error": "no sleep recorded for this date", **_errors_payload(errors)}
    offset = _offset_ms(dto.get("sleepStartTimestampGMT"), dto.get("sleepStartTimestampLocal"))

    def gmt_string_local(value: Any) -> datetime | None:
        try:
            moment = datetime.fromisoformat(str(value).replace(" ", "T").split(".")[0])
        except ValueError:
            return None
        return moment + timedelta(milliseconds=offset)

    stages: list[dict[str, Any]] = []
    for level in raw.get("sleepLevels") or []:
        if not isinstance(level, dict):
            continue
        stage = _SLEEP_STAGES.get(int(level.get("activityLevel") or 0))
        start, end = gmt_string_local(level.get("startGMT")), gmt_string_local(level.get("endGMT"))
        if start is None or end is None:
            continue
        if stages and stages[-1]["stage"] == stage and stages[-1]["_end"] == start:
            stages[-1]["_end"] = end
        else:
            stages.append({"stage": stage, "_start": start, "_end": end})
    timeline = [
        {"stage": s["stage"], "start": s["_start"].strftime("%H:%M"),
         "end": s["_end"].strftime("%H:%M"),
         "minutes": int(round((s["_end"] - s["_start"]).total_seconds() / 60))}
        for s in stages
    ]

    def ms_points(items: Any, key: str = "startGMT") -> list[tuple[Any, Any]]:
        return [(i.get(key), i.get("value")) for i in items or []
                if isinstance(i, dict) and i.get("value") is not None]

    hr_h = _hourly(ms_points(raw.get("sleepHeartRate")), offset, lambda v: v > 0)
    hrv_h = _hourly(ms_points(raw.get("hrvData")), offset, lambda v: v > 0)
    stress_h = _hourly(ms_points(raw.get("sleepStress")), offset, lambda v: v >= 0)
    bb_h = _hourly(ms_points(raw.get("sleepBodyBattery")), offset, lambda v: v >= 0)

    spo2_epochs = [
        (gmt_string_local(e.get("epochTimestamp")), e.get("spo2Reading"))
        for e in raw.get("wellnessEpochSPO2DataDTOList") or []
        if isinstance(e, dict) and isinstance(e.get("spo2Reading"), (int, float))
        and e.get("spo2Reading") > 0
    ]
    spo2_h: dict[str, list[float]] = defaultdict(list)
    for moment, value in spo2_epochs:
        if moment is not None:
            spo2_h[moment.strftime("%H:00")].append(float(value))
    lowest = min(spo2_epochs, key=lambda e: e[1], default=None)

    hours = sorted(set(hr_h) | set(hrv_h) | set(spo2_h) | set(stress_h),
                   key=lambda h: (int(h[:2]) < 12, h))
    hourly = [
        {
            "hour": h,
            "hr_avg": _mean(hr_h.get(h, []), 0),
            "hrv_avg_ms": _mean(hrv_h.get(h, []), 0),
            "spo2_avg": _mean(spo2_h.get(h, []), 0),
            "stress_avg": _mean(stress_h.get(h, []), 0),
            "body_battery_end": round_number(bb_h[h][-1], 0) if bb_h.get(h) else None,
        }
        for h in hours
    ]
    hrv_values = [float(v) for _, v in ms_points(raw.get("hrvData"))
                  if isinstance(v, (int, float))]
    restless = [p for p in ms_points(raw.get("sleepRestlessMoments"))]
    disruptions = [d for d in raw.get("breathingDisruptionData") or [] if isinstance(d, dict)]
    stage_minutes: dict[str, int] = defaultdict(int)
    for s in timeline:
        stage_minutes[s["stage"]] += s["minutes"]
    return {
        "date": day,
        "bedtime": _local_ms_to_iso(dto.get("sleepStartTimestampLocal")),
        "wake_time": _local_ms_to_iso(dto.get("sleepEndTimestampLocal")),
        "score": ((dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
        "stage_minutes": dict(stage_minutes),
        "stage_changes": len(timeline),
        "awakenings": sum(1 for s in timeline if s["stage"] == "awake"),
        "stage_timeline": timeline,
        "hrv": {
            "avg_ms": _mean(hrv_values),
            "min_ms": min(hrv_values, default=None),
            "max_ms": max(hrv_values, default=None),
        },
        "spo2": {
            "avg": dto.get("averageSpO2Value"),
            "lowest": lowest[1] if lowest else dto.get("lowestSpO2Value"),
            "lowest_at": lowest[0].strftime("%H:%M") if lowest and lowest[0] else None,
            "minutes_below_90": sum(1 for _, v in spo2_epochs if v < 90),
            "minutes_below_85": sum(1 for _, v in spo2_epochs if v < 85),
        },
        "restless_moments": len(restless),
        "breathing_disruptions": {
            "severity": dto.get("breathingDisruptionSeverity"),
            "events": len(disruptions),
        },
        "hourly": hourly,
        **_errors_payload(errors),
    }


def _pace_from_mps(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return _format_pace(1000 / value)


def _workout_target(step: dict[str, Any]) -> dict[str, Any] | None:
    kind = (step.get("targetType") or {}).get("workoutTargetTypeKey")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    if not kind or kind == "no.target":
        return None
    if kind == "pace.zone":
        paces = sorted(p for p in (_pace_from_mps(one), _pace_from_mps(two)) if p)
        return {"type": "pace", "range": paces}
    if kind == "heart.rate.zone" and step.get("zoneNumber"):
        return {"type": "heart_rate_zone", "zone": step.get("zoneNumber")}
    if kind == "power.zone" and step.get("zoneNumber"):
        return {"type": "power_zone", "zone": step.get("zoneNumber")}
    return {"type": kind.replace(".zone", ""), "range": [one, two]}


def _workout_end(step: dict[str, Any]) -> str | None:
    kind = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if kind == "time" and isinstance(value, (int, float)):
        return format_duration(value)
    if kind == "distance" and isinstance(value, (int, float)):
        return f"{value / 1000:g} km" if value >= 1000 else f"{value:g} m"
    if kind == "lap.button":
        return "lap button"
    return kind


def _flatten_steps(steps: Any) -> list[dict[str, Any]]:
    out = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_type = (step.get("stepType") or {}).get("stepTypeKey")
        if step.get("type") == "RepeatGroupDTO":
            out.append({"type": "repeat", "times": step.get("numberOfIterations"),
                        "steps": _flatten_steps(step.get("workoutSteps"))})
            continue
        out.append(
            {
                "type": step_type,
                "duration": _workout_end(step),
                "target": _workout_target(step),
                "description": (step.get("description") or "").strip() or None,
            }
        )
    return out


def get_workouts(
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum workouts to list.")] = 50,
) -> dict[str, Any]:
    """Saved workouts in the Garmin Connect library: id, name, sport, description and dates.
    Use get_workout_detail for the step-by-step structure."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint(f"workouts:{limit}", "workouts", lambda: client.get_workouts(0, limit), [],
                    errors)
    workouts = [
        {
            "workout_id": w.get("workoutId"),
            "name": w.get("workoutName"),
            "sport": (w.get("sportType") or {}).get("sportTypeKey"),
            "description": w.get("description"),
            "from_training_plan": bool(w.get("trainingPlanId") or w.get("atpPlanId")),
            "updated": str(w.get("updateDate") or "")[:10] or None,
        }
        for w in (raw if isinstance(raw, list) else [])
        if isinstance(w, dict)
    ]
    return {"count": len(workouts), "workouts": workouts, **_errors_payload(errors)}


def get_workout_detail(workout_id: int) -> dict[str, Any]:
    """Step-by-step structure of one saved workout: warm-up, intervals, recoveries, repeats,
    durations or distances, and pace/HR/power/cadence targets (pace in min/km)."""
    client = _client()
    errors: ErrorList = []
    raw = _endpoint(f"workout:{workout_id}", f"workout:{workout_id}",
                    lambda: client.get_workout_by_id(workout_id), {}, errors)
    if not isinstance(raw, dict) or not raw:
        return {"workout_id": workout_id, "error": "workout not found",
                **_errors_payload(errors)}
    segments = [s for s in raw.get("workoutSegments") or [] if isinstance(s, dict)]
    return {
        "workout_id": workout_id,
        "name": raw.get("workoutName"),
        "sport": (raw.get("sportType") or {}).get("sportTypeKey"),
        "description": raw.get("description"),
        "steps": [step for seg in segments for step in _flatten_steps(seg.get("workoutSteps"))],
        **_errors_payload(errors),
    }


def get_yearly_totals(
    year: Annotated[int, Field(ge=0, le=2100, description="Calendar year; 0 for current.")]
    = 0,
) -> dict[str, Any]:
    """Year-to-date (or full-year) totals per sport: activity count, distance, time,
    average and longest distance."""
    client = _client()
    errors: ErrorList = []
    today = today_iso()
    year = year or int(today[:4])
    start = f"{year}-01-01"
    end = min(today, f"{year}-12-31")

    def fetch(metric: str) -> dict[str, Any]:
        raw = _endpoint(f"progress:{metric}:{start}:{end}", f"progress:{metric}:{year}",
                        lambda: client.get_progress_summary_between_dates(start, end, metric,
                                                                         True),
                        [], errors)
        rows = raw if isinstance(raw, list) else []
        return (rows[0].get("stats") or {}) if rows and isinstance(rows[0], dict) else {}

    distance, duration = fetch("distance"), fetch("duration")
    sports = []
    for sport in sorted(set(distance) | set(duration)):
        d = (distance.get(sport) or {}).get("distance") or {}
        t = (duration.get(sport) or {}).get("duration") or {}
        # Garmin's progress summary reports distance in centimetres and time in milliseconds.
        sports.append(
            {
                "sport": sport,
                "activities": d.get("count") or t.get("count"),
                "distance_km": round_number((d.get("sum") or 0) / 100000, 1),
                "avg_distance_km": round_number((d.get("avg") or 0) / 100000, 2),
                "longest_km": round_number((d.get("max") or 0) / 100000, 2),
                "total_hours": round_number((t.get("sum") or 0) / 3600000, 1),
            }
        )
    sports.sort(key=lambda s: s["distance_km"] or 0, reverse=True)
    return {"year": year, "through": end, "sports": sports, **_errors_payload(errors)}


def get_badges(
    recent: Annotated[int, Field(ge=1, le=100, description="How many recent badges to list.")]
    = 15,
) -> dict[str, Any]:
    """Earned badges (count, points, most recent), badges in progress, and joined challenges
    with progress."""
    client = _client()
    errors: ErrorList = []
    earned = _endpoint("earned_badges", "earned_badges", lambda: client.get_earned_badges(), [],
                       errors)
    progress = _endpoint("in_progress_badges", "in_progress_badges",
                         lambda: client.get_in_progress_badges(), [], errors)
    challenges = _endpoint("badge_challenges", "badge_challenges",
                           lambda: client.get_badge_challenges(1, 50), [], errors)
    earned = [b for b in earned if isinstance(b, dict)] if isinstance(earned, list) else []
    earned.sort(key=lambda b: str(b.get("badgeEarnedDate")), reverse=True)

    def percent(b: dict[str, Any]) -> float | None:
        value, target = b.get("badgeProgressValue"), b.get("badgeTargetValue")
        if isinstance(value, (int, float)) and isinstance(target, (int, float)) and target:
            return round_number(min(value / target * 100, 100), 0)
        return None

    return {
        "earned_count": len(earned),
        "total_points": sum((b.get("badgePoints") or 0) * (b.get("badgeEarnedNumber") or 1)
                            for b in earned),
        "recent": [
            {"name": b.get("badgeName"), "earned": str(b.get("badgeEarnedDate") or "")[:10],
             "times_earned": b.get("badgeEarnedNumber"), "points": b.get("badgePoints")}
            for b in earned[:recent]
        ],
        "in_progress": [
            {"name": b.get("badgeName"), "progress_percent": percent(b),
             "progress": b.get("badgeProgressValue"), "target": b.get("badgeTargetValue")}
            for b in (progress if isinstance(progress, list) else [])
            if isinstance(b, dict) and (percent(b) or 0) < 100
        ],
        "challenges": [
            {"name": c.get("badgeChallengeName"), "start": str(c.get("startDate") or "")[:10],
             "end": str(c.get("endDate") or "")[:10], "progress_percent": percent(c),
             "completed": bool(c.get("badgeEarnedDate"))}
            for c in (challenges if isinstance(challenges, list) else [])
            if isinstance(c, dict) and c.get("userJoined")
        ],
        **_errors_payload(errors),
    }


TOOL_SPECS = [
    ToolSpec("get_recovery", get_recovery.__doc__ or "", get_recovery, {}),
    ToolSpec(
        "get_sleep",
        get_sleep.__doc__ or "",
        get_sleep,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_resting_heart_rate",
        get_resting_heart_rate.__doc__ or "",
        get_resting_heart_rate,
        {"days": {"type": "integer", "default": 30, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_stress",
        get_stress.__doc__ or "",
        get_stress,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_recent_activities",
        get_recent_activities.__doc__ or "",
        get_recent_activities,
        {
            "days": {
                "type": "integer",
                "default": 14,
                "minimum": 1,
                "maximum": MAX_DAYS_ACTIVITIES,
            },
            "limit": {
                "type": "integer",
                "default": 50,
                "minimum": 1,
                "maximum": MAX_ACTIVITY_LIMIT,
            },
            "start_date": {"type": "string", "default": ""},
            "end_date": {"type": "string", "default": ""},
        },
    ),
    ToolSpec(
        "get_activity_detail",
        get_activity_detail.__doc__ or "",
        get_activity_detail,
        {"activity_id": {"type": "integer"}},
    ),
    ToolSpec(
        "get_activity_splits",
        get_activity_splits.__doc__ or "",
        get_activity_splits,
        {"activity_id": {"type": "integer"}},
    ),
    ToolSpec(
        "get_recent_load",
        get_recent_load.__doc__ or "",
        get_recent_load,
        {"days": {"type": "integer", "default": 28, "minimum": 1, "maximum": MAX_DAYS_ACTIVITIES}},
    ),
    ToolSpec("get_training_load", get_training_load.__doc__ or "", get_training_load, {}),
    ToolSpec("get_fitness", get_fitness.__doc__ or "", get_fitness, {}),
    ToolSpec("get_zones", get_zones.__doc__ or "", get_zones, {}),
    ToolSpec("get_personal_records", get_personal_records.__doc__ or "", get_personal_records, {}),
    ToolSpec(
        "get_running_summary",
        get_running_summary.__doc__ or "",
        get_running_summary,
        {"days": {"type": "integer", "default": 90, "minimum": 1, "maximum": MAX_DAYS_ACTIVITIES}},
    ),
    ToolSpec(
        "get_monthly_running_stats",
        get_monthly_running_stats.__doc__ or "",
        get_monthly_running_stats,
        {"months": {"type": "integer", "default": 3, "minimum": 1, "maximum": 12}},
    ),
    ToolSpec(
        "get_health_summary",
        get_health_summary.__doc__ or "",
        get_health_summary,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_hrv_history",
        get_hrv_history.__doc__ or "",
        get_hrv_history,
        {"days": {"type": "integer", "default": 30, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_body_battery_range",
        get_body_battery_range.__doc__ or "",
        get_body_battery_range,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_body_composition",
        get_body_composition.__doc__ or "",
        get_body_composition,
        {"days": {"type": "integer", "default": 30, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_spo2",
        get_spo2.__doc__ or "",
        get_spo2,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_hydration",
        get_hydration.__doc__ or "",
        get_hydration,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec(
        "get_steps_history",
        get_steps_history.__doc__ or "",
        get_steps_history,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": MAX_DAYS_HEALTH}},
    ),
    ToolSpec("get_fitness_age", get_fitness_age.__doc__ or "", get_fitness_age, {}),
    ToolSpec(
        "download_activity_file",
        download_activity_file.__doc__ or "",
        download_activity_file,
        {
            "activity_id": {"type": "integer"},
            "save_path": {"type": "string", "default": ""},
        },
    ),
    ToolSpec(
        "get_full_snapshot",
        get_full_snapshot.__doc__ or "",
        get_full_snapshot,
        {
            "activity_days": {
                "type": "integer",
                "default": 14,
                "minimum": 1,
                "maximum": MAX_DAYS_ACTIVITIES,
            },
            "load_days": {
                "type": "integer",
                "default": 28,
                "minimum": 1,
                "maximum": MAX_DAYS_ACTIVITIES,
            },
        },
    ),
    ToolSpec(
        "get_activity_distance_splits",
        get_activity_distance_splits.__doc__ or "",
        get_activity_distance_splits,
        {
            "activity_id": {"type": "integer"},
            "split_km": {"type": "number", "default": 1.0, "minimum": 0.2, "maximum": 10},
        },
    ),
    ToolSpec(
        "get_daily_activity",
        get_daily_activity.__doc__ or "",
        get_daily_activity,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30}},
    ),
    ToolSpec(
        "get_body_battery",
        get_body_battery.__doc__ or "",
        get_body_battery,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30}},
    ),
    ToolSpec(
        "get_daily_vitals",
        get_daily_vitals.__doc__ or "",
        get_daily_vitals,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30}},
    ),
    ToolSpec(
        "get_training_readiness_history",
        get_training_readiness_history.__doc__ or "",
        get_training_readiness_history,
        {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30}},
    ),
    ToolSpec(
        "get_performance_metrics",
        get_performance_metrics.__doc__ or "",
        get_performance_metrics,
        {},
    ),
    ToolSpec(
        "get_race_prediction_trend",
        get_race_prediction_trend.__doc__ or "",
        get_race_prediction_trend,
        {"days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365}},
    ),
    ToolSpec(
        "get_running_tolerance",
        get_running_tolerance.__doc__ or "",
        get_running_tolerance,
        {"weeks": {"type": "integer", "default": 8, "minimum": 1, "maximum": 26}},
    ),
    ToolSpec("get_gear", get_gear.__doc__ or "", get_gear, {}),
    ToolSpec("get_devices", get_devices.__doc__ or "", get_devices, {}),
    ToolSpec(
        "get_body_metrics",
        get_body_metrics.__doc__ or "",
        get_body_metrics,
        {"days": {"type": "integer", "default": 90, "minimum": 1, "maximum": 365}},
    ),
    ToolSpec(
        "get_long_term_trends",
        get_long_term_trends.__doc__ or "",
        get_long_term_trends,
        {"weeks": {"type": "integer", "default": 12, "minimum": 2, "maximum": 52}},
    ),
    ToolSpec(
        "get_hrv_trend",
        get_hrv_trend.__doc__ or "",
        get_hrv_trend,
        {"days": {"type": "integer", "default": 30, "minimum": 7, "maximum": 90}},
    ),
    ToolSpec(
        "get_sleep_detail",
        get_sleep_detail.__doc__ or "",
        get_sleep_detail,
        {"date": {"type": "string", "default": ""}},
    ),
    ToolSpec(
        "get_daily_timeline",
        get_daily_timeline.__doc__ or "",
        get_daily_timeline,
        {"date": {"type": "string", "default": ""}},
    ),
    ToolSpec(
        "get_activity_segments",
        get_activity_segments.__doc__ or "",
        get_activity_segments,
        {"activity_id": {"type": "integer"}},
    ),
    ToolSpec(
        "get_workouts",
        get_workouts.__doc__ or "",
        get_workouts,
        {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}},
    ),
    ToolSpec(
        "get_workout_detail",
        get_workout_detail.__doc__ or "",
        get_workout_detail,
        {"workout_id": {"type": "integer"}},
    ),
    ToolSpec(
        "get_yearly_totals",
        get_yearly_totals.__doc__ or "",
        get_yearly_totals,
        {"year": {"type": "integer", "default": 0, "minimum": 0, "maximum": 2100}},
    ),
    ToolSpec(
        "get_badges",
        get_badges.__doc__ or "",
        get_badges,
        {"recent": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100}},
    ),
]

# Imported last: extended_tools builds on the helpers defined above.
from .extended_tools import EXTENDED_TOOL_SPECS  # noqa: E402

TOOL_SPECS.extend(EXTENDED_TOOL_SPECS)

from .coach import COACH_TOOL_SPECS  # noqa: E402

TOOL_SPECS.extend(COACH_TOOL_SPECS)

TOOLS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    required = [name for name, schema in spec.parameters.items() if "default" not in schema]
    return {
        "type": "object",
        "properties": spec.parameters,
        "required": required,
        "additionalProperties": False,
    }
