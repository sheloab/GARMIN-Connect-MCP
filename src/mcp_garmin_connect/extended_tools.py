"""Tools for the Garmin data that garminconnect >= 0.3.19 exposes beyond the core set.

Health Snapshot, ECG, blood pressure, glucose, cardio-respiratory statistics,
advanced running metrics, training-plan context and athlete profile, plus a
read-only escape hatch (``garmin_read``) for every other ``get_*`` method.

These endpoints are private Garmin Connect endpoints read through the
email/password backend; on the Personal API they report an error instead.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import date, timedelta
from typing import Annotated, Any

from pydantic import Field

from .errors import GarminConnectionError
from .garmin_client import get_manager, today_iso
from .tools import ErrorList, ToolSpec, _endpoint, _errors_payload, _without_gps

# Methods that must never reach the model: auth headers and FIT-file internals.
_DENIED_METHODS = {"get_api_headers", "get_format", "get_size"}
_MAX_RESPONSE_CHARS = 60_000
_WAVEFORM_KEY_RE = re.compile(r"(sample|waveform|lead|voltage|ecgData|rawData)", re.IGNORECASE)


def _ago(days: int) -> str:
    return (date.fromisoformat(today_iso()) - timedelta(days=days)).isoformat()


def _call(errors: ErrorList, method: str, *args: Any, **kwargs: Any) -> Any:
    """Fetch one garminconnect method through the shared cache and concurrency cap."""
    manager = get_manager()
    key = f"x:{method}:{json.dumps([args, kwargs], default=str, sort_keys=True)}"

    def fetch() -> Any:
        return getattr(manager.client(), method)(*args, **kwargs)

    try:
        return _endpoint(key, method, fetch, None, errors)
    except GarminConnectionError as exc:
        # garminconnect reports HTTP 4xx (e.g. an unsupported sport) as a connection
        # error; one rejected endpoint must not sink the whole multi-section tool.
        errors.append({"source": method, "error": str(exc)})
        return None


def _compact(value: Any, max_items: int = 60) -> Any:
    """Drop GPS fields and cap long lists so one response stays model-sized."""
    value = _without_gps(value)

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            if len(item) > max_items:
                return [walk(v) for v in item[:max_items]] + [
                    {"_truncated": f"{len(item) - max_items} more items omitted"}
                ]
            return [walk(v) for v in item]
        return item

    return walk(value)


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink list caps until the payload fits the response budget."""
    for cap in (60, 25, 10, 4):
        compacted = _compact(payload, cap)
        if len(json.dumps(compacted, default=str)) <= _MAX_RESPONSE_CHARS:
            return compacted
    text = json.dumps(_compact(payload, 2), default=str)
    return {"_truncated": True, "partial_json": text[:_MAX_RESPONSE_CHARS]}


def _without_waveform(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (f"<omitted: {len(v)} samples>" if isinstance(v, list) and len(v) > 20 else
                _without_waveform(v))
            if _WAVEFORM_KEY_RE.search(str(k)) else _without_waveform(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_waveform(v) for v in value]
    return value


def _session_ids(sessions: Any) -> list[tuple[str, str]]:
    rows = sessions if isinstance(sessions, list) else (
        sessions.get("healthSnapshots") or sessions.get("snapshots") or []
        if isinstance(sessions, dict) else []
    )
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        activity_uuid = row.get("activityUuid")
        uuid = (activity_uuid.get("uuid") if isinstance(activity_uuid, dict) else activity_uuid
                ) or row.get("snapshotUuid") or row.get("uuid") or row.get("snapshotId")
        day = str(row.get("calendarDate") or row.get("startTimestampLocal") or "")[:10]
        if uuid and day:
            found.append((day, str(uuid)))
    return found


# --- health -------------------------------------------------------------------------------


def get_health_snapshots(
    days: Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")] = 30,
    include_epochs: Annotated[
        bool, Field(description="Also fetch per-epoch samples (HR, HRV, SpO2) per session.")
    ] = False,
    max_sessions: Annotated[
        int, Field(ge=1, le=20, description="Sessions to expand with full detail.")
    ] = 5,
) -> dict[str, Any]:
    """Garmin Health Snapshot sessions (the 2-minute recording): heart rate, HRV, respiration,
    stress and SpO2 per session, newest first, with full detail for the latest sessions."""
    errors: ErrorList = []
    sessions = _call(errors, "get_health_snapshots_range", _ago(days - 1), today_iso())
    details = []
    for day, uuid in sorted(_session_ids(sessions), reverse=True)[:max_sessions]:
        entry = {"date": day, "snapshot_uuid": uuid,
                 "summary": _call(errors, "get_health_snapshot", day, uuid)}
        if include_epochs:
            entry["epochs"] = _call(errors, "get_health_snapshot_epochs", uuid)
        details.append(entry)
    return _fit({"period_days": days, "sessions": sessions, "details": details,
                 **_errors_payload(errors)})


def get_ecg_readings(
    days: Annotated[int, Field(ge=1, le=3650, description="Lookback window in days.")] = 365,
    ecg_id: Annotated[str, Field(description="One ECG recording id for full detail.")] = "",
    include_waveform: Annotated[
        bool, Field(description="Include the raw waveform samples (large).")
    ] = False,
) -> dict[str, Any]:
    """ECG app recordings: date, rhythm classification (e.g. sinus rhythm, AFib, inconclusive),
    average heart rate and logged symptoms. Pass ecg_id for one recording in detail.
    Informational only: Garmin's ECG result is not a diagnosis."""
    errors: ErrorList = []
    payload: dict[str, Any] = {"period_days": days}
    if ecg_id:
        detail = _call(errors, "get_ecg", ecg_id)
        payload["recording"] = detail if include_waveform else _without_waveform(detail)
    else:
        payload["recordings"] = _without_waveform(
            _call(errors, "get_ecg_list", _ago(days - 1), today_iso(), 100)
        )
    return _fit({**payload, **_errors_payload(errors)})


def get_blood_pressure(
    days: Annotated[int, Field(ge=1, le=365, description="Lookback window in days.")] = 30,
) -> dict[str, Any]:
    """Blood pressure: last reading per day (systolic, diastolic, pulse, category) over the
    window plus 52 weekly averages."""
    errors: ErrorList = []
    return _fit({
        "period_days": days,
        "daily_last": _call(errors, "get_daily_last_blood_pressure", _ago(days - 1), today_iso()),
        "weekly": _call(errors, "get_weekly_blood_pressure", today_iso()),
        **_errors_payload(errors),
    })


def get_cardio_respiratory(
    days: Annotated[int, Field(ge=1, le=90, description="Lookback window in days.")] = 14,
) -> dict[str, Any]:
    """Daily resting/min/max heart rate, waking and sleeping respiration, SpO2 acclimation,
    7-day resting HR average and weekly HR averages including abnormal heart-rate alerts."""
    errors: ErrorList = []
    start, end = _ago(days - 1), today_iso()
    return _fit({
        "period_days": days,
        "heart_rate_daily": _call(errors, "get_daily_heart_rate_stats", start, end),
        "respiration_daily": _call(errors, "get_daily_respiration_stats", start, end),
        "spo2_acclimation": _call(errors, "get_acclimation_stats", start, end),
        "resting_hr_7d_avg": _call(errors, "get_week_average_resting_hr", end),
        "heart_rate_weekly": _call(errors, "get_weekly_heart_rate_stats", end, 12),
        **_errors_payload(errors),
    })


def get_sleep_stats(
    days: Annotated[int, Field(ge=1, le=365, description="Window for overall averages.")] = 30,
    weeks: Annotated[int, Field(ge=1, le=52, description="Weekly history length.")] = 12,
) -> dict[str, Any]:
    """Sleep statistics: averages over the window (duration, score, stages, HRV, respiration),
    weekly sleep history, and today's naps."""
    errors: ErrorList = []
    end = today_iso()
    return _fit({
        "period_days": days,
        "overall": _call(errors, "get_sleep_stats_overall", _ago(days - 1), end),
        "weekly": _call(errors, "get_sleep_stats_weekly", end, weeks),
        "naps_today": _call(errors, "get_naps", end),
        **_errors_payload(errors),
    })


def get_glucose(
    days: Annotated[int, Field(ge=1, le=90, description="Lookback window in days.")] = 14,
) -> dict[str, Any]:
    """Logged blood glucose readings (manual entries or a connected CGM)."""
    errors: ErrorList = []
    return _fit({
        "period_days": days,
        "readings": _call(errors, "get_glucose_data_range", _ago(days - 1), today_iso()),
        **_errors_payload(errors),
    })


def get_wellness_trends(
    weeks: Annotated[int, Field(ge=1, le=52, description="Weekly history length.")] = 12,
) -> dict[str, Any]:
    """Long-range wellness: weekly calories, floors, hydration, fitness age, daily stress,
    body battery and intensity-minute series, monthly steps and lifetime totals."""
    errors: ErrorList = []
    end = today_iso()
    start = _ago(weeks * 7 - 1)
    return _fit({
        "weeks": weeks,
        "calories_weekly": _call(errors, "get_weekly_calories_stats", end, weeks),
        "floors_weekly": _call(errors, "get_weekly_floors_stats", end, weeks),
        "hydration_weekly": _call(errors, "get_weekly_hydration_stats", end, weeks),
        "fitness_age_weekly": _call(errors, "get_fitness_age_weekly", end, weeks),
        "stress_daily": _call(errors, "get_daily_stress_stats", start, end),
        "body_battery_daily": _call(errors, "get_daily_body_battery_stats", start, end),
        "intensity_minutes_daily": _call(errors, "get_daily_intensity_minutes_stats", start, end),
        "steps_monthly": _call(errors, "get_monthly_steps", end, 12),
        "lifetime_totals": _call(errors, "get_lifetime_totals"),
        **_errors_payload(errors),
    })


# --- performance and coaching -------------------------------------------------------------


def get_advanced_performance(
    days: Annotated[int, Field(ge=7, le=365, description="History window in days.")] = 90,
) -> dict[str, Any]:
    """Coaching metrics: running economy, stamina curve, running FTP history,
    heat/altitude acclimation, 4-week load balance (aerobic low/high, anaerobic),
    weekly training status, readiness history, morning readiness, current biometrics
    (VO2 max, lactate threshold, FTP), fitness-age targets, cycling ability and swim CSS."""
    errors: ErrorList = []
    start, end = _ago(days - 1), today_iso()
    return _fit({
        "period_days": days,
        "current_biometrics": _call(errors, "get_current_biometrics"),
        "running_economy": _call(errors, "get_running_economy", end),
        "running_economy_history": _call(errors, "get_running_economy_range", start, end),
        "stamina_curve_running": _call(errors, "get_stamina_curve", end, "running"),
        "ftp_history_running": _call(
            errors, "get_functional_threshold_power_range", start, end, sport="RUNNING"
        ),
        "heat_altitude_acclimation": _call(errors, "get_heat_altitude_acclimation", end),
        "four_week_load_balance": _call(errors, "get_training_four_week_load_balance", end),
        "training_status_weekly": _call(errors, "get_weekly_training_status", start, end),
        "training_readiness_history": _call(
            errors, "get_training_readiness_range", _ago(min(days, 28) - 1), end
        ),
        "morning_training_readiness": _call(errors, "get_morning_training_readiness", end),
        "fitness_age_details": _call(errors, "get_fitness_age_details", end),
        "cycling_ability": _call(errors, "get_cycling_ability", end),
        "critical_swim_speed": _call(errors, "get_critical_swim_speed", end),
        **_errors_payload(errors),
    })


def get_training_plan_context(
    days_ahead: Annotated[
        int, Field(ge=1, le=180, description="How far ahead to read scheduled workouts.")
    ] = 28,
    lookback_days: Annotated[
        int, Field(ge=30, le=730, description="Window for fastest splits.")
    ] = 365,
) -> dict[str, Any]:
    """What a coach needs before writing a program: primary race and upcoming events,
    active Garmin training plans (incl. adaptive Garmin Coach), scheduled workouts ahead,
    the next scheduled workout, PacePro bands, fastest running splits and PR candidates."""
    errors: ErrorList = []
    today = today_iso()
    ahead = (date.fromisoformat(today) + timedelta(days=days_ahead)).isoformat()
    return _fit({
        "primary_event": _call(errors, "get_primary_event"),
        "upcoming_events": _call(errors, "get_upcoming_events", 180, 10),
        "active_training_plans": _call(errors, "get_active_training_plans", today),
        "scheduled_workouts": _call(errors, "get_scheduled_workouts_by_period", today, ahead),
        "next_scheduled_workout": _call(errors, "get_next_scheduled_workout"),
        "pace_bands": _call(errors, "get_pace_bands"),
        "fastest_running_splits": _call(
            errors, "get_fastest_splits", _ago(lookback_days - 1), today, "running"
        ),
        "personal_record_candidates": _call(errors, "get_personal_record_candidates"),
        **_errors_payload(errors),
    })


def get_activity_extras(activity_id: int) -> dict[str, Any]:
    """Extra detail for one activity: laps, structured-workout step results, strength sets,
    power curve, gear used, badges and personal records it earned (GPS removed)."""
    errors: ErrorList = []
    return _fit({
        "activity_id": activity_id,
        "laps": _call(errors, "get_activity_laps", activity_id),
        "workout_steps": _call(errors, "get_activity_step_details", activity_id),
        "exercise_sets": _call(errors, "get_activity_exercise_sets", activity_id),
        "power_curve": _call(errors, "get_activity_power_curve", activity_id),
        "gear": _call(errors, "get_activity_gear_v2", activity_id),
        "badges": _call(errors, "get_badges_for_activity", activity_id),
        "personal_records": _call(errors, "get_personal_records_for_activity", activity_id),
        **_errors_payload(errors),
    })


def get_athlete_profile() -> dict[str, Any]:
    """Athlete profile for coaching: age/sex/height/weight, VO2 max, lactate threshold,
    HR zones per sport (v2), power zones, unit system, timezone, primary training device,
    paired sensors, supported features and last sync time."""
    errors: ErrorList = []
    return _fit({
        "personal_information": _call(errors, "get_personal_information"),
        "heart_rate_zones": _call(errors, "get_heart_rate_zones_v2"),
        "power_zones": _call(errors, "get_power_zones"),
        "unit_system": _call(errors, "get_unit_system"),
        "timezone": _call(errors, "get_user_timezone"),
        "primary_training_device": _call(errors, "get_primary_training_device"),
        "sensors": _call(errors, "get_sensors"),
        "usage_indicators": _call(errors, "get_usage_indicators"),
        "last_sync": _call(errors, "get_sync_timestamp"),
        **_errors_payload(errors),
    })


# --- generic read-only access -------------------------------------------------------------


def _readable_methods() -> dict[str, Any]:
    from garminconnect import Garmin

    return {
        name: member
        for name, member in inspect.getmembers(Garmin, inspect.isfunction)
        if name.startswith("get_") and name not in _DENIED_METHODS
    }


def list_garmin_methods(
    query: Annotated[str, Field(description="Filter by name or description, e.g. 'sleep'.")] = "",
) -> dict[str, Any]:
    """Every read-only Garmin Connect method garmin_read can call, with its parameters and a
    one-line description. Use it to find data no dedicated tool covers."""
    needle = query.strip().lower()
    methods = []
    for name, fn in sorted(_readable_methods().items()):
        doc = (inspect.getdoc(fn) or "").split("\n")[0]
        if needle and needle not in name.lower() and needle not in doc.lower():
            continue
        signature = str(inspect.signature(fn)).replace("(self, ", "(").replace("(self)", "()")
        methods.append({"method": name, "parameters": signature, "description": doc})
    return {"count": len(methods), "methods": methods}


def garmin_read(
    method: Annotated[str, Field(description="A get_* method name from list_garmin_methods.")],
    params_json: Annotated[
        str, Field(description='Keyword arguments as JSON, e.g. {"cdate": "2026-10-29"}.')
    ] = "",
) -> dict[str, Any]:
    """Call any read-only Garmin Connect method by name (only get_* methods; nothing that
    edits, uploads or deletes). Results are cached, GPS-free and size-capped."""
    allowed = _readable_methods()
    if method not in allowed:
        return {"error": f"unknown or not allowed method: {method!r}",
                "hint": "call list_garmin_methods to see the allowed names"}
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except ValueError as exc:
        return {"error": f"params_json is not valid JSON: {exc}"}
    if not isinstance(params, dict):
        return {"error": "params_json must be a JSON object"}
    try:
        inspect.signature(allowed[method]).bind(None, **params)
    except TypeError as exc:
        return {"error": f"bad parameters for {method}: {exc}",
                "parameters": str(inspect.signature(allowed[method]))}
    errors: ErrorList = []
    result = _call(errors, method, **params)
    return _fit({"method": method, "params": params, "result": result,
                 **_errors_payload(errors)})


EXTENDED_TOOL_SPECS = [
    ToolSpec("get_health_snapshots", get_health_snapshots.__doc__ or "", get_health_snapshots, {
        "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
        "include_epochs": {"type": "boolean", "default": False},
        "max_sessions": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
    }),
    ToolSpec("get_ecg_readings", get_ecg_readings.__doc__ or "", get_ecg_readings, {
        "days": {"type": "integer", "default": 365, "minimum": 1, "maximum": 3650},
        "ecg_id": {"type": "string", "default": ""},
        "include_waveform": {"type": "boolean", "default": False},
    }),
    ToolSpec("get_blood_pressure", get_blood_pressure.__doc__ or "", get_blood_pressure, {
        "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
    }),
    ToolSpec("get_cardio_respiratory", get_cardio_respiratory.__doc__ or "",
             get_cardio_respiratory, {
        "days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90},
    }),
    ToolSpec("get_sleep_stats", get_sleep_stats.__doc__ or "", get_sleep_stats, {
        "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
        "weeks": {"type": "integer", "default": 12, "minimum": 1, "maximum": 52},
    }),
    ToolSpec("get_glucose", get_glucose.__doc__ or "", get_glucose, {
        "days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90},
    }),
    ToolSpec("get_wellness_trends", get_wellness_trends.__doc__ or "", get_wellness_trends, {
        "weeks": {"type": "integer", "default": 12, "minimum": 1, "maximum": 52},
    }),
    ToolSpec("get_advanced_performance", get_advanced_performance.__doc__ or "",
             get_advanced_performance, {
        "days": {"type": "integer", "default": 90, "minimum": 7, "maximum": 365},
    }),
    ToolSpec("get_training_plan_context", get_training_plan_context.__doc__ or "",
             get_training_plan_context, {
        "days_ahead": {"type": "integer", "default": 28, "minimum": 1, "maximum": 180},
        "lookback_days": {"type": "integer", "default": 365, "minimum": 30, "maximum": 730},
    }),
    ToolSpec("get_activity_extras", get_activity_extras.__doc__ or "", get_activity_extras, {
        "activity_id": {"type": "integer"},
    }),
    ToolSpec("get_athlete_profile", get_athlete_profile.__doc__ or "", get_athlete_profile, {}),
    ToolSpec("list_garmin_methods", list_garmin_methods.__doc__ or "", list_garmin_methods, {
        "query": {"type": "string", "default": ""},
    }),
    ToolSpec("garmin_read", garmin_read.__doc__ or "", garmin_read, {
        "method": {"type": "string"},
        "params_json": {"type": "string", "default": ""},
    }),
]
