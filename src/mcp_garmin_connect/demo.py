"""Offline demo client with fictional data.

``garmin-mcp serve --demo`` (or ``GARMIN_DEMO=1``) swaps the real Garmin client
for this deterministic stand-in so the server can be tried — or recorded for
demos — without credentials and without ever touching real health data.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _days_between(start: str, end: str) -> list[str]:
    """Inclusive list of ISO dates, oldest first, like the range endpoints return."""
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    days: list[str] = []
    current = first
    while current <= last:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _activity(
    offset: int,
    name: str,
    type_key: str,
    km: float,
    seconds: int,
    hr_avg: int,
    hr_max: int,
    cadence: int,
    aerobic_te: float,
    anaerobic_te: float = 0.0,
) -> dict[str, Any]:
    day = _day(offset)
    return {
        "activityId": 900000 + offset,
        "activityName": name,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": f"{day} 06:30:00",
        "duration": float(seconds),
        "movingDuration": float(seconds),
        "distance": km * 1000,
        "averageHR": hr_avg,
        "maxHR": hr_max,
        "averageRunningCadenceInStepsPerMinute": cadence,
        "averageSpeed": round((km * 1000) / seconds, 3),
        "trainingEffect": aerobic_te,
        "anaerobicTrainingEffect": anaerobic_te,
        "trainingEffectLabel": "TEMPO" if aerobic_te >= 3.5 else "BASE",
        "aerobicTrainingEffectMessage": "MINOR_IMPROVEMENT",
        "anaerobicTrainingEffectMessage": "NO_ANAEROBIC_BENEFIT",
    }


_DEMO_ACTIVITIES = [
    _activity(1, "Easy Run", "running", 6.2, 2320, 148, 163, 171, 3.0),
    _activity(3, "Track Intervals 6x400m", "track_running", 8.0, 2850, 162, 182, 176, 3.8, 2.1),
    _activity(5, "Morning Run", "running", 5.0, 1880, 146, 158, 170, 2.8),
    _activity(7, "Weekend Long Run", "running", 12.0, 4740, 152, 168, 169, 3.4),
    _activity(9, "Recovery Run", "running", 4.0, 1600, 138, 149, 168, 2.2),
    _activity(12, "Tempo Run", "running", 7.5, 2640, 158, 175, 174, 3.9, 0.8),
]

_DEMO_PERSONAL_RECORDS = [
    {"typeId": 3, "value": "00:28:10", "prStartTimeGmtFormatted": _day(120)},
    {"typeId": 4, "value": "00:59:40", "prStartTimeGmtFormatted": _day(210)},
    {"typeId": 5, "value": "02:12:00", "prStartTimeGmtFormatted": _day(300)},
    {"typeId": 7, "value": "12.00", "prStartTimeGmtFormatted": _day(7)},
]

_DEMO_PR_TYPES = [
    {"id": 3, "key": "pr.label.5k.run", "sport": "RUNNING", "visible": True},
    {"id": 4, "key": "pr.label.10k.run", "sport": "RUNNING", "visible": True},
    {"id": 5, "key": "pr.label.half.marathon", "sport": "RUNNING", "visible": True},
    {"id": 7, "key": "pr.label.longest.run", "sport": "RUNNING", "visible": True},
]


class DemoGarmin:
    """Deterministic stand-in for ``garminconnect.Garmin`` with fictional data."""

    display_name = "demo-athlete"
    backend_name = "demo"

    def login(self, tokenstore: str | None = None) -> None:
        return None

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        return [
            {
                "score": 78,
                "level": "MODERATE",
                "feedbackShort": "TRAINING_READY",
                "feedbackLong": "You are ready for a moderate session today.",
                "recoveryTime": 660,  # minutes (the tool converts to hours)
                "sleepScore": 82,
                "hrvWeeklyAverage": 44,
            }
        ]

    def get_training_status(self, cdate: str) -> dict[str, Any]:
        load_balance = {
            "acuteTrainingLoad": 312.0,
            "chronicTrainingLoad": 285.0,
            "acuteChronicWorkloadRatio": 1.09,
            "acwrStatus": "OPTIMAL",
        }
        return {
            "mostRecentTrainingLoadBalance": load_balance,
            "mostRecentTrainingStatus": {
                "latestTrainingStatusData": {
                    "demo-device": {
                        "trainingStatus": "PRODUCTIVE",
                        "trainingStatusFeedbackPhrase": "PRODUCTIVE_2",
                        "fitnessTrend": 1,
                        "acuteTrainingLoadDTO": {
                            "dailyTrainingLoadAcute": 312.0,
                            "dailyTrainingLoadChronic": 285.0,
                            "dailyAcuteChronicWorkloadRatio": 1.09,
                            "acwrStatus": "OPTIMAL",
                        },
                    }
                }
            },
        }

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        return {"hrvSummary": {"lastNightAvg": 44, "weeklyAvg": 44, "status": "BALANCED"}}

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 26700,
                "sleepScores": {"overall": {"value": 82}},
            }
        }

    def get_body_battery(self, start: str, end: str) -> list[dict[str, Any]]:
        # One entry per day, like Garmin: the date field is what the range tool keys on.
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
        days: list[dict[str, Any]] = []
        current = first
        while current <= last:
            days.append(
                {
                    "date": f"{current.isoformat()}T00:00:00.000",
                    "charged": 62,
                    "drained": 49,
                    "highestBatteryLevel": 86,
                    "lowestBatteryLevel": 21,
                    "endOfDayBatteryLevel": 37,
                }
            )
            current += timedelta(days=1)
        return days

    def get_rhr_day(self, cdate: str) -> dict[str, Any]:
        return {
            "allMetrics": {
                "metricsMap": {
                    "WELLNESS_RESTING_HEART_RATE": [{"calendarDate": cdate, "value": 58}]
                }
            }
        }

    def get_all_day_stress(self, cdate: str) -> dict[str, Any]:
        return {
            "avgStressLevel": 24,
            "maxStressLevel": 71,
            "restStressDuration": 36000,
            "lowStressDuration": 16000,
            "mediumStressDuration": 5200,
            "highStressDuration": 900,
            "activityStressDuration": 5400,
        }

    def get_activities(self, start: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return list(_DEMO_ACTIVITIES)[start : start + limit]

    def get_activity(self, activity_id: int) -> dict[str, Any]:
        for activity in _DEMO_ACTIVITIES:
            if activity["activityId"] == activity_id:
                return {
                    "activityId": activity_id,
                    "activityName": activity["activityName"],
                    "activityType": activity["activityType"],
                    "summaryDTO": activity,
                }
        return {}

    def get_max_metrics(self, cdate: str) -> list[dict[str, Any]]:
        return [{"generic": {"vo2MaxPreciseValue": 47.0, "vo2MaxValue": 47}, "cycling": {}}]

    def get_race_predictions(self) -> list[dict[str, Any]]:
        return [
            {
                "time5K": 1660,
                "time10K": 3480,
                "timeHalfMarathon": 7640,
                "timeMarathon": 15900,
            }
        ]

    def get_cycling_ftp(self) -> dict[str, Any]:
        return {"functionalThresholdPower": 210}

    def get_user_profile(self) -> dict[str, Any]:
        return {
            "userData": {
                "heartRateZones": [
                    {"zone": 1, "low": 96, "high": 124},
                    {"zone": 2, "low": 125, "high": 148},
                    {"zone": 3, "low": 149, "high": 162},
                    {"zone": 4, "low": 163, "high": 175},
                    {"zone": 5, "low": 176, "high": 190},
                ],
                "lactateThresholdHeartRate": 166,
                "thresholdHeartRate": 166,
            }
        }

    def get_personal_record(self) -> dict[str, Any]:
        return {"personalRecords": list(_DEMO_PERSONAL_RECORDS)}

    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return [
            {
                "sport": "DEFAULT",
                "trainingMethod": "HR_MAX",
                "maxHeartRateUsed": 190,
                "restingHeartRateUsed": 58,
                "lactateThresholdHeartRateUsed": 166,
                "zone1Floor": 96,
                "zone2Floor": 125,
                "zone3Floor": 149,
                "zone4Floor": 163,
                "zone5Floor": 176,
            }
        ]

    def get_power_zones_for_sport(self, sport: str) -> dict[str, Any]:
        return {
            "sport": sport,
            "functionalThresholdPower": 300.0,
            "zone1Floor": 195.0,
            "zone2Floor": 240.0,
            "zone3Floor": 270.0,
            "zone4Floor": 300.0,
            "zone5Floor": 345.0,
        }

    def connectapi(self, url: str) -> Any:
        if "personalrecordtype" in url:
            return list(_DEMO_PR_TYPES)
        return {}

    # -- Personal API-only tools (fictional data, same shapes) -------------
    def get_body_composition(self, start: str, end: str) -> dict[str, Any]:
        return {
            "dateWeightList": [
                {
                    "calendarDate": _day(offset),
                    "weight": 71200 + offset * 100,
                    "bmi": 23.8,
                    "bodyFat": 19.4,
                    "muscleMass": 33200,
                    "bodyWater": 57.2,
                }
                for offset in range(0, 7)
            ]
        }

    def get_spo2(self, cdate: str) -> dict[str, Any]:
        return {"averageSpO2": 96.0, "lowestSpO2": 91.0, "latestSpO2": 97.0}

    def get_hydration(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            {"calendarDate": day, "valueInML": 2400, "goalInML": 3000}
            for day in _days_between(start, end)
        ]

    def get_steps(self, start: str, end: str) -> list[dict[str, Any]]:
        days = _days_between(start, end)
        return [
            {"calendarDate": day, "totalSteps": 8400 - index * 120, "stepGoal": 8000}
            for index, day in enumerate(days)
        ]

    def get_fitness_age(self, cdate: str | None = None) -> dict[str, Any]:
        return {
            "fitnessAge": 19.4,
            "chronologicalAge": 21,
            "achievableFitnessAge": 18.2,
            "previousFitnessAge": 19.8,
        }

    def download_activity_file(self, activity_id: int) -> bytes:
        # Not a real FIT file: enough for dry runs and tests to prove the write path.
        return b"PK\x03\x04demo-zip" + str(activity_id).encode()
