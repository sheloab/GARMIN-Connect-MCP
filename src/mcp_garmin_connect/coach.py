"""Evidence-based running plans and air quality for the athlete's usual running areas.

``build_running_plan`` turns the athlete's own Garmin history (weekly volume, longest
recent run, race predictions, HR zones, readiness) into a periodized plan. The rules it
applies, and the studies behind them, are listed in ``EVIDENCE`` and returned with every
plan so the reasoning can be checked.

``get_running_air_quality`` estimates where the athlete usually runs from activity start
points, rounded to a grid (``AIR_QUALITY_GRID_DEG``, default 0.01 deg ~ 1 km), and sends
only those rounded points to the Open-Meteo air-quality API. Exact GPS never leaves the
process and never reaches the model.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated, Any

import httpx
from pydantic import Field

from .garmin_client import get_manager, today_iso
from .normalizers import activity_type_key, sport_bucket
from .tools import (
    ErrorList,
    ToolSpec,
    _activities_paged,
    _errors_payload,
    get_fitness,
    get_recovery,
    get_zones,
)

OPEN_METEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_AQ_HOURLY = "pm2_5,pm10,ozone,nitrogen_dioxide,us_aqi"
_RUN_HOURS = range(5, 22)
_PLAN_WEEKS = (2, 4, 8, 12, 16)

GOALS: dict[str, dict[str, Any]] = {
    "10k": {"label": "10K", "km": 10.0, "prediction": "10k", "peak_km": 40.0,
            "long_run_cap_km": 16.0, "taper_weeks": 1},
    "half_marathon": {"label": "Half marathon", "km": 21.0975, "prediction": "half_marathon",
                      "peak_km": 50.0, "long_run_cap_km": 21.0, "taper_weeks": 2},
    "marathon": {"label": "Marathon", "km": 42.195, "prediction": "marathon",
                 "peak_km": 65.0, "long_run_cap_km": 32.0, "taper_weeks": 2},
}

EVIDENCE: list[dict[str, str]] = [
    {
        "id": "single_session_spike",
        "finding": "In 5,200 runners tracked with Garmin devices, a single run more than 10% "
        "longer than the longest run of the previous 30 days was associated with a higher rate "
        "of overuse injury; week-to-week change and acute:chronic ratio were not.",
        "applied": "Each long run grows at most 10% over the longest run of the last 30 days.",
        "source": "Frandsen JSB et al. How much running is too much? Identifying high-risk "
        "running sessions in a 5200-person cohort study. Br J Sports Med 2025;59:1203-1210.",
    },
    {
        "id": "ten_percent_rule",
        "finding": "A randomized trial of 532 novice runners found the same injury rate with a "
        "10%-per-week graded program as with a faster standard program (20.8% vs 20.3%).",
        "applied": "The 10% weekly rule is not treated as protective; weekly volume grows "
        "moderately with a lighter week every 4th week, and the long-run cap does the "
        "injury-specific work.",
        "source": "Buist I et al. No effect of a graded training program on the number of "
        "running-related injuries in novice runners: a randomized controlled trial. "
        "Am J Sports Med 2008;36(1):33-39.",
    },
    {
        "id": "weekly_progression",
        "finding": "Runners increasing weekly distance by more than 30% over two weeks appeared "
        "more vulnerable to distance-related injuries, although the overall difference was "
        "not statistically significant.",
        "applied": "Weekly volume grows about 8% per full week (never above 10%); a lighter "
        "week every 4th week, after which volume returns to the last full week's level.",
        "source": "Nielsen RO et al. Excessive progression in weekly running distance and risk "
        "of running-related injuries: an association which varies according to type of "
        "injury. J Orthop Sports Phys Ther 2014;44(10):739-747.",
    },
    {
        "id": "intensity_distribution",
        "finding": "Successful endurance athletes do roughly 75-80% of training at low "
        "intensity; in a randomized comparison, a polarized distribution improved key "
        "endurance variables more than threshold-, volume- or HIIT-heavy blocks.",
        "applied": "About 80% of weekly running is easy (HR zone 1-2); at most two quality "
        "sessions per week.",
        "source": "Seiler KS, Kjerland GO. Scand J Med Sci Sports 2006;16(1):49-56. "
        "Stoggl T, Sperlich B. Polarized training has greater impact on key endurance "
        "variables than threshold, high intensity, or high volume training. Front Physiol "
        "2014;5:33.",
    },
    {
        "id": "taper",
        "finding": "Meta-analysis: a ~2-week taper with volume reduced 41-60% in an "
        "exponential pattern is the most efficient way to maximize performance gains.",
        "applied": "Half/marathon plans end with a 2-week taper (about 70% then 45% of peak "
        "volume, intensity kept); 10K plans taper 1 week.",
        "source": "Bosquet L et al. Effects of tapering on performance: a meta-analysis. "
        "Med Sci Sports Exerc 2007;39(8):1358-1365.",
    },
    {
        "id": "strength",
        "finding": "Systematic review: strength training improves running economy, time-trial "
        "and sprint performance in middle- and long-distance runners.",
        "applied": "Two strength sessions per week in base/build, one in peak/taper.",
        "source": "Blagrove RC et al. Effects of strength training on the physiological "
        "determinants of middle- and long-distance running performance: a systematic "
        "review. Sports Med 2018;48(5):1117-1149.",
    },
    {
        "id": "hrv_guided",
        "finding": "Adjusting each day's training to that morning's HRV improved fitness at "
        "least as well as a fixed plan.",
        "applied": "Daily rule: low readiness or unbalanced/low HRV turns that day's quality "
        "session into an easy run or rest.",
        "source": "Kiviniemi AM et al. Endurance training guided individually by daily heart "
        "rate variability measurements. Eur J Appl Physiol 2007;101(6):743-751.",
    },
    {
        "id": "air_pollution",
        "finding": "Exercise raises the inhaled dose of particulate matter and ozone, with "
        "pulmonary and cardiovascular effects; the advice is to reduce exposure, not to "
        "stop exercising.",
        "applied": "Quality sessions move to the cleanest hours; US AQI > 100 makes them easy "
        "or indoor, > 150 indoor or rest. PM2.5 is compared with the WHO 24-hour guideline "
        "of 15 ug/m3.",
        "source": "Giles LV, Koehle MS. The health effects of exercising in air pollution. "
        "Sports Med 2014;44(2):223-249. WHO global air quality guidelines, 2021. "
        "US EPA AirNow, Air Quality Index guidance.",
    },
    {
        "id": "heat",
        "finding": "Exertional heat stroke is a medical emergency; most heat illness is "
        "preventable by recognising and modifying risk factors.",
        "applied": "In heat, run early, slow to HR targets rather than pace, hydrate, and stop "
        "at warning signs (confusion, dizziness, stopping sweating).",
        "source": "Roberts WO et al. ACSM expert consensus statement on exertional heat "
        "illness: recognition, management, and return to activity. Curr Sports Med Rep "
        "2023;22(4):134-149.",
    },
    {
        "id": "training_paces",
        "finding": "Training paces derived from current race performance (VDOT) set easy, "
        "marathon, threshold, interval and repetition intensities.",
        "applied": "Paces come from the Daniels-Gilbert VDOT equations applied to Garmin's "
        "race prediction; HR zones come from the athlete's own Garmin zones.",
        "source": "Daniels J. Daniels' Running Formula, 4th ed. Human Kinetics. "
        "Daniels J, Gilbert J. Oxygen Power, 1979.",
    },
]


# --- shared helpers -----------------------------------------------------------------------


def _runs(days: int, errors: ErrorList) -> list[dict[str, Any]]:
    start = (date.fromisoformat(today_iso()) - timedelta(days=days - 1)).isoformat()
    runs = []
    for item in _activities_paged(400, errors, stop_before=start):
        day = str(item.get("startTimeLocal") or "")[:10]
        if day >= start and sport_bucket(activity_type_key(item)) == "run":
            runs.append(item)
    return runs


def _hms(seconds: float | None) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _parse_time(text: str) -> float | None:
    parts = text.strip().split(":")
    if not text.strip() or not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    value = 0.0
    for part in parts:
        value = value * 60 + int(part)
    return value if len(parts) > 1 else None


# --- VDOT (Daniels & Gilbert) -------------------------------------------------------------


def vdot_from_race(distance_m: float, seconds: float) -> float | None:
    if distance_m <= 0 or seconds <= 0:
        return None
    minutes = seconds / 60
    velocity = distance_m / minutes
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity**2
    fraction = (0.8 + 0.1894393 * math.exp(-0.012778 * minutes)
                + 0.2989558 * math.exp(-0.1932605 * minutes))
    return vo2 / fraction


def _velocity_for_vo2(vo2: float) -> float:
    a, b, c = 0.000104, 0.182258, -(4.60 + vo2)
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def _pace(vdot: float, fraction: float) -> str:
    velocity = _velocity_for_vo2(vdot * fraction)  # metres per minute
    return f"{_hms(60_000 / velocity)}/km"


def training_paces(vdot: float) -> dict[str, str]:
    """Pace ranges by Daniels intensity (fractions of VDOT, approximate)."""
    return {
        "easy": f"{_pace(vdot, 0.74)} - {_pace(vdot, 0.62)}",
        "marathon": f"{_pace(vdot, 0.84)} - {_pace(vdot, 0.79)}",
        "threshold": f"{_pace(vdot, 0.88)} - {_pace(vdot, 0.85)}",
        "interval": f"{_pace(vdot, 1.00)} - {_pace(vdot, 0.96)}",
        "repetition": f"{_pace(vdot, 1.10)} - {_pace(vdot, 1.05)}",
    }


# --- plan generation ----------------------------------------------------------------------


def _history(runs: list[dict[str, Any]], weeks: int = 12) -> dict[str, Any]:
    today = date.fromisoformat(today_iso())
    weekly = [0.0] * weeks
    counts = [0] * weeks
    longest_30d = 0.0
    for run in runs:
        day = date.fromisoformat(str(run.get("startTimeLocal"))[:10])
        km = (run.get("distance") or 0) / 1000
        index = (today - day).days // 7
        if 0 <= index < weeks:
            weekly[weeks - 1 - index] += km
            counts[weeks - 1 - index] += 1
        if (today - day).days < 30:
            longest_30d = max(longest_30d, km)
    recent = weekly[-4:]
    return {
        "weekly_km_oldest_first": [round(v, 1) for v in weekly],
        "runs_per_week_oldest_first": counts,
        "avg_weekly_km_4w": round(sum(recent) / 4, 1),
        "avg_weekly_km_12w": round(sum(weekly) / weeks, 1),
        "avg_runs_per_week_4w": round(sum(counts[-4:]) / 4, 1),
        "longest_run_30d_km": round(longest_30d, 1),
    }


def recommend_weeks(goal: str, base_km: float, longest_km: float) -> tuple[int, str]:
    """Recommended plan length from the current base; returns (weeks, reason)."""
    if goal == "marathon":
        if base_km >= 50 and longest_km >= 24:
            return 12, "solid marathon base (>=50 km/week, long run >=24 km)"
        if base_km >= 30 and longest_km >= 16:
            return 16, "moderate base: 16 weeks to build the long run safely to ~32 km"
        return 16, ("base is low for a marathon: 16 weeks is the minimum offered here; "
                    "consider a half marathon first or 8+ weeks of base building")
    if goal == "half_marathon":
        if base_km >= 35 and longest_km >= 15:
            return 8, "solid half-marathon base (>=35 km/week, long run >=15 km)"
        if base_km >= 20 and longest_km >= 10:
            return 12, "moderate base: 12 weeks to build the long run to ~21 km"
        return 16, "low base: 16 weeks so volume and long run grow gradually"
    if base_km >= 25 and longest_km >= 10:
        return 8, "solid 10K base (>=25 km/week, long run >=10 km)"
    if base_km >= 12:
        return 8, "moderate base: 8 weeks of progressive build"
    return 12, "low base: 12 weeks so the body adapts to regular running"


def _phases(weeks: int, taper: int) -> list[str]:
    if weeks <= 2:
        return ["sharpen", "taper"][-weeks:] if weeks == 2 else ["taper"]
    taper = min(taper, weeks - 1)
    rest = weeks - taper
    peak = max(1, round(rest * 0.2))
    build = max(1, round(rest * 0.4)) if rest - peak > 1 else 0
    base = rest - peak - build
    return ["base"] * base + ["build"] * build + ["peak"] * peak + ["taper"] * taper


def _volumes(weeks: int, phases: list[str], base_km: float, peak_target: float) -> list[float]:
    start = max(base_km, 10.0)
    peak_target = max(peak_target, start)
    volumes: list[float] = []
    current = start
    after_step_back = False
    for index, phase in enumerate(phases):
        if phase == "taper":
            taper_index = phases[index:].count("taper")
            peak = max(volumes) if volumes else current
            volumes.append(peak * (0.45 if taper_index == 1 else 0.70))
            continue
        if index > 0 and (index + 1) % 4 == 0 and phase != "sharpen":
            volumes.append(current * 0.75)  # step-back week
            after_step_back = True
            continue
        if index > 0 and not after_step_back:
            current = min(current * 1.08, peak_target)
        after_step_back = False  # the week after a step-back returns to the last full week
        volumes.append(current)
    return [round(v, 1) for v in volumes]


_QUALITY = {
    "10k": {"base": ["strides", "hills"], "build": ["threshold", "intervals"],
            "peak": ["intervals", "race_pace"], "sharpen": ["intervals", "race_pace"],
            "taper": ["race_pace"]},
    "half_marathon": {"base": ["strides", "hills"], "build": ["threshold", "intervals"],
                      "peak": ["threshold", "race_pace"], "sharpen": ["threshold", "race_pace"],
                      "taper": ["race_pace"]},
    "marathon": {"base": ["strides", "hills"], "build": ["threshold", "marathon_pace"],
                 "peak": ["threshold", "marathon_pace"], "sharpen": ["marathon_pace"],
                 "taper": ["marathon_pace"]},
}

_SESSION_TEXT = {
    "strides": ("Easy run + 6-8 x 20 s strides (relaxed fast, full recovery)", "easy", 0),
    "hills": ("Easy run + 6-10 x 45-60 s uphill at hard effort, jog down", "repetition", 3),
    "threshold": ("2 km warm-up, 3-4 x 8-10 min at threshold, 2 min jog, 2 km cool-down",
                  "threshold", 7),
    "intervals": ("2 km warm-up, 5-6 x 3 min at interval pace, 2-3 min jog, 2 km cool-down",
                  "interval", 6),
    "race_pace": ("2 km warm-up, 3-4 x 2 km at goal race pace, 2 min jog, 2 km cool-down",
                  "race", 8),
    "marathon_pace": ("2 km warm-up, 8-14 km at marathon pace, 2 km cool-down",
                      "marathon", 12),
}

_HR_ZONE_FOR = {"easy": "Z1-Z2", "marathon": "Z3", "threshold": "Z4", "interval": "Z4-Z5",
                "repetition": "Z5", "race": "Z3-Z4"}


def _week_sessions(goal: str, phase: str, volume: float, long_run: float, days: int,
                   paces: dict[str, str] | None, race_pace: str | None) -> list[dict[str, Any]]:
    quality_count = 0 if days <= 2 else 1 if days == 3 or phase == "taper" else 2
    quality = _QUALITY[goal][phase][:quality_count]
    sessions: list[dict[str, Any]] = []
    used = long_run
    for kind in quality:
        text, intensity, work_km = _SESSION_TEXT[kind]
        km = round(min(work_km + 4, max(6.0, volume * 0.2)), 1) if work_km else 6.0
        used += km
        pace = race_pace if intensity == "race" else (paces or {}).get(intensity)
        sessions.append({"type": kind, "km": km, "description": text,
                         "pace": pace, "hr_zone": _HR_ZONE_FOR.get(intensity)})
    easy_runs = max(0, days - 1 - len(quality))
    easy_km = max(0.0, volume - used)
    for _ in range(easy_runs):
        sessions.append({"type": "easy", "km": round(easy_km / easy_runs, 1) if easy_runs else 0,
                         "description": "Easy conversational run",
                         "pace": (paces or {}).get("easy"), "hr_zone": "Z1-Z2"})
    long_text = "Long run, easy"
    if goal == "marathon" and phase in {"peak", "sharpen"}:
        long_text = "Long run with the final 25-35% at marathon pace"
    sessions.append({"type": "long_run", "km": round(long_run, 1), "description": long_text,
                     "pace": (paces or {}).get("easy"), "hr_zone": "Z2"})
    return sessions


def _race_week(spec: dict[str, Any], days: int, paces: dict[str, str] | None,
               race_pace: str | None) -> list[dict[str, Any]]:
    """Race week: one short tune-up early, short easy runs, then the race."""
    easy_km = 4.0 if spec["km"] < 15 else 5.0
    sessions: list[dict[str, Any]] = [{
        "type": "race_pace", "km": 6.0,
        "description": "Early in the week: 2 km warm-up, 3 x 1 km at race pace, 2 km cool-down",
        "pace": race_pace, "hr_zone": "Z3-Z4"}]
    for _ in range(max(0, days - 2)):
        sessions.append({"type": "easy", "km": easy_km,
                         "description": "Short easy run (add 4 strides the day before the race)",
                         "pace": (paces or {}).get("easy"), "hr_zone": "Z1-Z2"})
    sessions.append({"type": "race", "km": round(spec["km"], 1),
                     "description": f"{spec['label']} race", "pace": race_pace, "hr_zone": None})
    return sessions


def build_running_plan(
    goal: Annotated[str, Field(description="Goal race: '10k', 'half_marathon' or 'marathon'.")],
    weeks: Annotated[
        int, Field(ge=0, le=16, description="Plan length: 2, 4, 8, 12 or 16; 0 = recommend.")
    ] = 0,
    days_per_week: Annotated[int, Field(ge=3, le=7, description="Running days per week.")] = 4,
    goal_time: Annotated[str, Field(description="Target finish time H:MM:SS, optional.")] = "",
    race_date: Annotated[str, Field(description="Race date YYYY-MM-DD, optional.")] = "",
) -> dict[str, Any]:
    """Evidence-based, periodized running plan built from the athlete's Garmin history.

    Call with weeks=0 first: it returns the recommended plan length (from the last 12 weeks
    of running, the longest recent run and the goal) and the options, so the athlete can
    choose. The plan gives weekly volume, long run, sessions with VDOT paces and the
    athlete's HR zones, strength work, adjustment rules and the cited evidence."""
    errors: ErrorList = []
    if goal not in GOALS:
        return {"error": f"goal must be one of {sorted(GOALS)}"}
    spec = GOALS[goal]
    runs = _runs(84, errors)
    history = _history(runs)
    base, longest = history["avg_weekly_km_4w"], history["longest_run_30d_km"]
    recommended, reason = recommend_weeks(goal, base, longest)

    weeks_to_race = None
    if race_date:
        try:
            weeks_to_race = max(1, (date.fromisoformat(race_date)
                                    - date.fromisoformat(today_iso())).days // 7)
        except ValueError:
            return {"error": "race_date must be YYYY-MM-DD"}
    options = {
        "recommended_weeks": recommended,
        "reason": reason,
        "weeks_to_race": weeks_to_race,
        "choices": list(_PLAN_WEEKS),
        "notes": {
            2: "sharpening + taper only: for someone already race-ready",
            4: "short build: maintain fitness and sharpen",
            8: "one full build cycle",
            12: "classic build with base, build, peak and taper",
            16: "longest option: most gradual, best for a low base or a first marathon",
        },
    }
    if weeks == 0:
        return {"goal": spec["label"], "history": history, "plan_length": options,
                "next_step": "Ask the athlete which length to use (and running days per week, "
                "goal time, race date), then call again with weeks set.",
                **_errors_payload(errors)}
    if weeks not in _PLAN_WEEKS:
        return {"error": f"weeks must be one of {list(_PLAN_WEEKS)} (or 0 to recommend)"}

    fitness = get_fitness()
    zones = get_zones()
    recovery = get_recovery()
    prediction = ((fitness.get("race_predictions") or {}).get(spec["prediction"]) or {})
    predicted = prediction.get("seconds")
    vdot = vdot_from_race(spec["km"] * 1000, predicted) if predicted else None
    target_seconds = _parse_time(goal_time) if goal_time else None
    race_seconds = target_seconds or predicted
    race_pace = f"{_hms(race_seconds / spec['km'])}/km" if race_seconds else None
    paces = training_paces(vdot) if vdot else None

    phases = _phases(weeks, spec["taper_weeks"])
    volumes = _volumes(weeks, phases, base, spec["peak_km"])
    start = date.fromisoformat(today_iso())
    start += timedelta(days=(7 - start.weekday()) % 7 or 7)
    plan = []
    longest_so_far = max(longest, 5.0)
    for index, (phase, volume) in enumerate(zip(phases, volumes, strict=True)):
        if phase == "taper":
            long_run = longest_so_far * (0.6 if phases[index:].count("taper") > 1 else 0.0)
        else:
            target = min(spec["long_run_cap_km"], volume * 0.4)
            long_run = min(target, longest_so_far * 1.10) if index else min(target, longest_so_far)
            if (index + 1) % 4 == 0:
                long_run = min(long_run, longest_so_far * 0.8)
            longest_so_far = max(longest_so_far, long_run)
        is_race_week = index == weeks - 1
        sessions = _week_sessions(goal, phase, volume, max(long_run, 0.0), days_per_week,
                                  paces, race_pace)
        if is_race_week:
            sessions = _race_week(spec, days_per_week, paces, race_pace)
            volume = round(sum(s["km"] for s in sessions), 1)
        plan.append({
            "week": index + 1,
            "starts": (start + timedelta(weeks=index)).isoformat(),
            "phase": phase,
            "step_back_week": phase not in {"taper", "sharpen"} and index > 0
            and (index + 1) % 4 == 0,
            "target_km": volume,
            "sessions": sessions,
            "strength": "2 x 30-40 min (heavy compound lifts + plyometrics)"
            if phase in {"base", "build"} else "1 x 20-30 min maintenance",
        })

    readiness = recovery.get("training_readiness") if isinstance(recovery, dict) else None
    hrv_status = ((recovery.get("hrv") or {}).get("status_latest")
                  if isinstance(recovery, dict) else None)
    warnings = []
    if weeks < recommended:
        warnings.append(f"{weeks} weeks is shorter than the recommended {recommended} for your "
                        f"current base ({reason}).")
    if weeks_to_race and weeks_to_race != weeks:
        warnings.append(f"The race is {weeks_to_race} weeks away; align the plan's last week "
                        "with race week.")
    if goal == "marathon" and base < 30:
        warnings.append("Current volume is low for a marathon; extra base weeks are advised.")
    if not runs:
        warnings.append("No running found in the last 12 weeks: start with run-walk and "
                        "see a doctor first if you have health conditions.")
    return {
        "goal": spec["label"],
        "goal_time": goal_time or None,
        "predicted_time": _hms(predicted),
        "vdot": round(vdot, 1) if vdot else None,
        "training_paces": paces,
        "race_pace": race_pace,
        "heart_rate_zones": zones.get("heart_rate_zones") if isinstance(zones, dict) else None,
        "history": history,
        "plan_length": options,
        "current_state": {"training_readiness": readiness, "hrv_status": hrv_status,
                          "training_status": recovery.get("training_status")
                          if isinstance(recovery, dict) else None},
        "weeks": plan,
        "daily_adjustment_rules": [
            "Training readiness below ~40, HRV status LOW/UNBALANCED, or poor sleep: replace "
            "that day's quality session with an easy run or rest; do not make it up later.",
            "Pain that changes your gait or lasts into the next day: stop and rest; seek a "
            "physiotherapist if it persists beyond a few days.",
            "Never make a single run more than 10% longer than your longest run of the last "
            "30 days.",
            "US AQI > 100: quality sessions become easy or indoor; > 150: treadmill or rest. "
            "Check get_running_air_quality for the cleanest hours.",
            "Heat: run early, follow HR rather than pace, drink to thirst plus electrolytes on "
            "long runs; stop at dizziness or confusion.",
            "Missed a week: repeat the previous week instead of jumping ahead.",
        ],
        "warnings": warnings,
        "evidence": EVIDENCE,
        "disclaimer": "General training guidance from your Garmin data, not medical advice. "
        "Get medical clearance for heart, lung or other conditions, or ECG/blood-pressure "
        "flags.",
        **_errors_payload(errors),
    }


# --- air quality --------------------------------------------------------------------------


def _fetch_air_quality(params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=20) as client:
        response = client.get(OPEN_METEO_AQ_URL, params=params)
        response.raise_for_status()
        return response.json()


def _air_quality(lat: float, lon: float, errors: ErrorList, **extra: Any) -> dict[str, Any]:
    manager = get_manager()
    params = {"latitude": lat, "longitude": lon, "hourly": _AQ_HOURLY, "timezone": "auto",
              **extra}
    key = "air_quality:" + ":".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        return manager.cached(key, lambda: _fetch_air_quality(params))
    except Exception as exc:  # noqa: BLE001 - one area failing must not kill the report
        errors.append({"source": "open_meteo_air_quality", "error": f"{type(exc).__name__}: "
                       f"{exc}"})
        return {}


def _hourly_table(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    table = {}
    for i, stamp in enumerate(times):
        table[stamp[:13]] = {k: (hourly.get(k) or [None] * len(times))[i]
                             for k in _AQ_HOURLY.split(",")}
    return table


def _aqi_category(aqi: float | None) -> str | None:
    if aqi is None:
        return None
    for limit, label in ((50, "good"), (100, "moderate"), (150, "unhealthy for sensitive groups"),
                         (200, "unhealthy"), (300, "very unhealthy")):
        if aqi <= limit:
            return label
    return "hazardous"


def _mean(values: list[Any], digits: int = 1) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(numbers), digits) if numbers else None


def get_running_air_quality(
    days: Annotated[int, Field(ge=7, le=365, description="Running history to analyse.")] = 90,
    forecast_days: Annotated[int, Field(ge=0, le=5, description="Days of forecast.")] = 3,
    max_areas: Annotated[int, Field(ge=1, le=5, description="Usual running areas to report.")] = 3,
) -> dict[str, Any]:
    """Air quality where you usually run: finds your most common running areas from activity
    start points (rounded to a ~1 km grid, exact GPS is never shown), then reports the
    PM2.5, PM10, ozone, NO2 and US AQI you actually ran in, the cleanest hours of the day
    per area, and a forecast with the best run windows. Data: Open-Meteo (CAMS)."""
    settings = get_manager().settings
    if not settings.air_quality_enabled:
        return {"error": "air quality is disabled (AIR_QUALITY_ENABLED=0)"}
    grid = settings.air_quality_grid_deg
    errors: ErrorList = []
    runs = _runs(days, errors)
    cells: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        lat, lon = run.get("startLatitude"), run.get("startLongitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            cell = (round(round(lat / grid) * grid, 4), round(round(lon / grid) * grid, 4))
            cells[cell].append(run)
    if not cells:
        return {"period_days": days, "runs_analysed": len(runs), "areas": [],
                "note": "no running activities with a start location in this window",
                **_errors_payload(errors)}

    located = sum(len(v) for v in cells.values())
    areas = []
    ranked = sorted(cells.items(), key=lambda item: len(item[1]), reverse=True)[:max_areas]
    today = today_iso()
    for rank, ((lat, lon), area_runs) in enumerate(ranked, start=1):
        first = min(str(r.get("startTimeLocal"))[:10] for r in area_runs)
        history = _hourly_table(_air_quality(lat, lon, errors, start_date=first, end_date=today))
        exposures = []
        for run in area_runs:
            stamp = str(run.get("startTimeLocal") or "")
            try:
                begin = datetime.fromisoformat(stamp.replace(" ", "T"))
            except ValueError:
                continue
            hours = max(1, math.ceil((run.get("duration") or 0) / 3600))
            rows = [history.get((begin + timedelta(hours=h)).strftime("%Y-%m-%dT%H"))
                    for h in range(hours)]
            rows = [r for r in rows if r]
            if not rows:
                continue
            exposures.append({
                "date": stamp[:10], "start_hour": begin.hour,
                "us_aqi": _mean([r["us_aqi"] for r in rows], 0),
                "pm2_5": _mean([r["pm2_5"] for r in rows]),
                "ozone": _mean([r["ozone"] for r in rows]),
            })
        # US AQI is a rolling 24-hour index, so it barely moves hour to hour; rank the
        # hours of the day by the hourly PM2.5 concentration instead.
        by_hour: dict[int, list[float]] = defaultdict(list)
        for stamp, row in history.items():
            hour = int(stamp[11:13])
            if hour in _RUN_HOURS and isinstance(row["pm2_5"], (int, float)):
                by_hour[hour].append(row["pm2_5"])
        hour_profile = {h: _mean(v) for h, v in sorted(by_hour.items())}
        cleanest = sorted((h for h in hour_profile if hour_profile[h] is not None),
                          key=lambda h: hour_profile[h])[:3]
        aqis = [e["us_aqi"] for e in exposures if e["us_aqi"] is not None]
        area = {
            "area": f"area_{rank}",
            "grid_point": {"lat": lat, "lon": lon, "grid_deg": grid},
            "share_of_runs_pct": round(100 * len(area_runs) / located),
            "runs": len(area_runs),
            "during_your_runs": {
                "median_us_aqi": round(statistics.median(aqis)) if aqis else None,
                "max_us_aqi": max(aqis) if aqis else None,
                "category_median": _aqi_category(statistics.median(aqis)) if aqis else None,
                "runs_aqi_over_100": sum(1 for a in aqis if a > 100),
                "runs_aqi_over_150": sum(1 for a in aqis if a > 150),
                "mean_pm2_5": _mean([e["pm2_5"] for e in exposures]),
                "runs_pm2_5_over_who_15": sum(
                    1 for e in exposures if isinstance(e["pm2_5"], (int, float))
                    and e["pm2_5"] > 15),
                "usual_start_hours": [h for h, _ in Counter(
                    e["start_hour"] for e in exposures).most_common(3)],
            },
            "cleanest_hours_historically": [
                {"hour": h, "avg_pm2_5": hour_profile[h]} for h in cleanest],
            "avg_pm2_5_by_hour": hour_profile,
            "recent_runs": sorted(exposures, key=lambda e: e["date"], reverse=True)[:15],
        }
        if forecast_days:
            forecast = _hourly_table(_air_quality(lat, lon, errors, forecast_days=forecast_days))
            days_out: dict[str, list[tuple[int, Any, Any]]] = defaultdict(list)
            for stamp, row in forecast.items():
                if stamp[:10] >= today and int(stamp[11:13]) in _RUN_HOURS:
                    days_out[stamp[:10]].append((int(stamp[11:13]), row["pm2_5"], row["us_aqi"]))
            area["forecast"] = [
                {
                    "date": day,
                    "us_aqi_range": [min(a for _, _, a in hours if isinstance(a, (int, float))),
                                     max(a for _, _, a in hours if isinstance(a, (int, float)))]
                    if any(isinstance(a, (int, float)) for _, _, a in hours) else None,
                    "best_hours": [{"hour": h, "pm2_5": p, "us_aqi": a} for h, p, a in sorted(
                        (x for x in hours if isinstance(x[1], (int, float))),
                        key=lambda x: x[1])[:3]],
                    "hours_over_100": [h for h, _, a in hours
                                       if isinstance(a, (int, float)) and a > 100],
                }
                for day, hours in sorted(days_out.items())
            ]
        areas.append(area)
    return {
        "period_days": days,
        "runs_analysed": len(runs),
        "runs_with_location": located,
        "areas": areas,
        "guidance": {
            "us_aqi_0_50": "good: any session",
            "us_aqi_51_100": "moderate: fine for most; unusually sensitive people ease off",
            "us_aqi_101_150": "sensitive groups (asthma, heart disease): reduce prolonged or "
            "heavy exertion; make quality sessions easy or indoor",
            "us_aqi_over_150": "everyone: avoid prolonged or heavy exertion outdoors",
            "who_pm2_5_24h_guideline_ug_m3": 15,
        },
        "data_source": "Open-Meteo Air Quality API (Copernicus CAMS models). Model grid is "
        "about 10 km in Europe and 40 km globally, so nearby areas share values.",
        "privacy": f"Only grid points rounded to {grid} deg were sent to Open-Meteo.",
        **_errors_payload(errors),
    }


COACH_TOOL_SPECS = [
    ToolSpec("build_running_plan", build_running_plan.__doc__ or "", build_running_plan, {
        "goal": {"type": "string"},
        "weeks": {"type": "integer", "default": 0, "minimum": 0, "maximum": 16},
        "days_per_week": {"type": "integer", "default": 4, "minimum": 3, "maximum": 7},
        "goal_time": {"type": "string", "default": ""},
        "race_date": {"type": "string", "default": ""},
    }),
    ToolSpec("get_running_air_quality", get_running_air_quality.__doc__ or "",
             get_running_air_quality, {
        "days": {"type": "integer", "default": 90, "minimum": 7, "maximum": 365},
        "forecast_days": {"type": "integer", "default": 3, "minimum": 0, "maximum": 5},
        "max_areas": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5},
    }),
]
