from __future__ import annotations

from datetime import datetime
from typing import Any

SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}
BIKE_TYPES = {
    "cycling",
    "indoor_cycling",
    "road_biking",
    "mountain_biking",
    "gravel_cycling",
    "virtual_ride",
    "e_bike_mountain",
    "e_bike_fitness",
}
RUN_TYPES = {"running", "treadmill_running", "trail_running", "track_running", "virtual_run"}
STRENGTH_TYPES = {"strength_training", "indoor_climbing", "bouldering"}
MINDFULNESS_TYPES = {"yoga", "pilates", "meditation", "breathwork", "mindfulness"}
HIIT_TYPES = {"hiit", "cardio", "indoor_cardio", "fitness_equipment"}
WALK_TYPES = {"walking", "indoor_walking", "hiking"}


def round_number(value: Any, ndigits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def parse_local_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T")).isoformat()
    except ValueError:
        return value


def format_duration(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def pace_from_speed(speed_mps: Any) -> str | None:
    if not isinstance(speed_mps, (int, float)) or speed_mps <= 0:
        return None
    seconds_per_km = 1000.0 / speed_mps
    minutes = int(seconds_per_km // 60)
    seconds = int(seconds_per_km % 60)
    return f"{minutes}:{seconds:02d}"


def sport_bucket(type_key: str | None) -> str | None:
    if not type_key:
        return None
    if type_key in SWIM_TYPES:
        return "swim"
    if type_key in BIKE_TYPES:
        return "bike"
    if type_key in RUN_TYPES:
        return "run"
    if type_key in STRENGTH_TYPES:
        return "strength"
    if type_key in MINDFULNESS_TYPES:
        return "mindfulness"
    if type_key in HIIT_TYPES:
        return "hiit"
    if type_key in WALK_TYPES:
        return "walk"
    return "other"


def activity_type_key(activity: dict[str, Any]) -> str | None:
    activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    if isinstance(activity_type, dict):
        return activity_type.get("typeKey")
    if isinstance(activity_type, str):
        return activity_type
    return None


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any] | None:
    type_key = activity_type_key(activity)
    sport = sport_bucket(type_key)
    if not sport:
        return None

    duration_s = activity.get("duration") or activity.get("movingDuration") or 0
    distance_m = activity.get("distance") or 0
    hr_avg = activity.get("averageHR") or activity.get("averageHr")
    hr_max = activity.get("maxHR") or activity.get("maxHr")

    out: dict[str, Any] = {
        "activity_id": activity.get("activityId") or activity.get("activity_id"),
        "name": activity.get("activityName") or activity.get("name"),
        "sport": sport,
        "type_key": type_key,
        "date": parse_local_datetime(activity.get("startTimeLocal") or activity.get("start_time")),
        "duration_min": round_number(duration_s / 60, 1),
        "duration": format_duration(duration_s),
        "distance_km": round_number(distance_m / 1000, 2) if distance_m else None,
        "hr_avg": int(hr_avg) if isinstance(hr_avg, (int, float)) else None,
        "hr_max": int(hr_max) if isinstance(hr_max, (int, float)) else None,
    }

    pace = pace_from_speed(activity.get("averageSpeed"))
    if sport == "run" and pace:
        out["pace_min_km"] = pace
    if sport == "run":
        cadence = activity.get("averageRunningCadenceInStepsPerMinute") or activity.get(
            "avgRunCadence"
        )
        if isinstance(cadence, (int, float)):
            out["cadence_spm"] = round_number(cadence, 0)
    if sport == "bike":
        power = activity.get("avgPower") or activity.get("averagePower")
        if isinstance(power, (int, float)):
            out["avg_power_w"] = int(power)
        normalized_power = activity.get("normPower") or activity.get("normalizedPower")
        if isinstance(normalized_power, (int, float)):
            out["normalized_power_w"] = int(normalized_power)
    if sport == "swim":
        strokes = activity.get("totalNumberOfStrokes") or activity.get("strokes")
        if isinstance(strokes, (int, float)):
            out["total_strokes"] = int(strokes)
    return out
